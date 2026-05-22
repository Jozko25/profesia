#!/usr/bin/env python3
"""Poll profesia.sk for new jobs matching keywords, notify via Telegram."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import httpx
import yaml
from selectolax.parser import HTMLParser

BASE = "https://www.profesia.sk"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"


@dataclass
class Job:
    id: str
    title: str
    url: str
    employer: str
    location: str
    posted: str
    matched_keyword: str


def fetch(client: httpx.Client, url: str) -> str:
    r = client.get(url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.text


def parse_listing(html: str, keyword: str) -> list[Job]:
    tree = HTMLParser(html)
    jobs: list[Job] = []
    for row in tree.css("li.list-row"):
        a = row.css_first("h2 a")
        if not a:
            continue

        href = a.attributes.get("href", "")
        offer_id = (a.attributes.get("id") or "").removeprefix("offer")
        if not offer_id or not href:
            continue

        title_node = a.css_first("span.title")
        employer_node = row.css_first("span.employer")
        loc_node = row.css_first("span.job-location")
        posted_node = row.css_first("span.info strong")

        jobs.append(Job(
            id=offer_id,
            title=(title_node.text(strip=True) if title_node else a.text(strip=True)),
            url=urljoin(BASE, href),
            employer=employer_node.text(strip=True) if employer_node else "",
            location=loc_node.text(strip=True) if loc_node else "",
            posted=posted_node.text(strip=True) if posted_node else "",
            matched_keyword=keyword,
        ))
    return jobs


def search(client: httpx.Client, keyword: str, max_pages: int) -> list[Job]:
    out: list[Job] = []
    for page in range(1, max_pages + 1):
        url = f"{BASE}/praca/?search_anywhere={quote_plus(keyword)}&sort_by=relevance"
        if page > 1:
            url += f"&page_num={page}"
        html = fetch(client, url)
        page_jobs = parse_listing(html, keyword)
        if not page_jobs:
            break
        out.extend(page_jobs)
        time.sleep(1.0)  # be polite
    return out


def llm_score(job: Job, cfg: dict) -> int:
    """Return 0-10 relevance score from OpenAI, or 10 if disabled."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print(f"  [llm] OPENAI_API_KEY not set, skipping filter", file=sys.stderr)
        return 10

    prompt = (
        f"Profile: {cfg['profile'].strip()}\n\n"
        f"Job:\nTitle: {job.title}\nEmployer: {job.employer}\n"
        f"Location: {job.location}\n\n"
        "Rate this job's relevance 0-10. Respond with ONLY an integer."
    )
    try:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4,
                "temperature": 0,
            },
            timeout=30,
        )
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"].strip()
        return int("".join(c for c in txt if c.isdigit()) or "0")
    except Exception as e:
        print(f"  [llm] error: {e}", file=sys.stderr)
        return 10  # fail open


def notify_telegram(jobs: list[Job]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, printing instead:", file=sys.stderr)
        for j in jobs:
            print(f"  - {j.title} @ {j.employer} ({j.location}) :: {j.url}")
        return

    for j in jobs:
        text = (
            f"<b>{escape(j.title)}</b>\n"
            f"{escape(j.employer)}\n"
            f"{escape(j.location)}\n"
            f"<i>{escape(j.posted)}</i> · matched: <code>{escape(j.matched_keyword)}</code>\n"
            f"{j.url}"
        )
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"telegram error {r.status_code}: {r.text}", file=sys.stderr)
        time.sleep(0.5)


def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # cap to last 5000 to prevent unbounded growth
    state["seen"] = state["seen"][-5000:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yml").read_text())
    state = load_state()
    seen = set(state["seen"])

    all_jobs: dict[str, Job] = {}
    with httpx.Client(http2=False) as client:
        for kw in cfg["keywords"]:
            print(f"[search] {kw}")
            try:
                results = search(client, kw, cfg.get("max_pages", 2))
            except Exception as e:
                print(f"  error: {e}", file=sys.stderr)
                continue
            for j in results:
                if j.id not in all_jobs:
                    all_jobs[j.id] = j

    new_jobs = [j for jid, j in all_jobs.items() if jid not in seen]
    print(f"[stats] total={len(all_jobs)} new={len(new_jobs)} seen={len(seen)}")

    # LLM filter (optional)
    llm_cfg = cfg.get("llm_filter", {})
    to_notify: list[Job] = []
    if llm_cfg.get("enabled") and new_jobs:
        min_score = llm_cfg.get("min_score", 6)
        for j in new_jobs:
            score = llm_score(j, llm_cfg)
            print(f"  [score={score}] {j.title}")
            if score >= min_score:
                to_notify.append(j)
    else:
        to_notify = new_jobs

    if to_notify:
        notify_telegram(to_notify)
    else:
        print("[notify] nothing new")

    # Mark ALL found jobs as seen (even filtered-out ones, so we don't re-rate)
    state["seen"] = list(seen | set(all_jobs.keys()))
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
