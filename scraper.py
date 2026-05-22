#!/usr/bin/env python3
"""Poll profesia.sk for new jobs matching keywords, notify via Telegram, render dashboard."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
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
STATE_FILE = ROOT / "state" / "jobs.json"
LEGACY_STATE = ROOT / "state" / "seen.json"
DOCS_FILE = ROOT / "docs" / "index.html"


@dataclass
class Job:
    id: str
    title: str
    url: str
    employer: str
    location: str
    posted: str
    posted_days: int          # parsed from data-dimension13, -1 if unknown
    salary: str               # raw text, "" if absent
    matched_keyword: str
    first_seen: str = ""      # ISO timestamp, set on insertion


def fetch(client: httpx.Client, url: str) -> str:
    r = client.get(url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.text


def build_search_url(keyword: str, page: int, filters: dict) -> str:
    """Compose listing URL with path-based + query filters."""
    parts = ["praca"]
    if filters.get("region"):
        parts.append(filters["region"])
    if filters.get("employment_type"):
        parts.append(filters["employment_type"])
    path = "/" + "/".join(parts) + "/"

    qs = [f"search_anywhere={quote_plus(keyword)}", "sort_by=relevance"]
    if filters.get("min_salary"):
        qs.append(f"salary={int(filters['min_salary'])}")
        qs.append(f"salary_period={filters.get('salary_period', 'm')}")
    if filters.get("remote") is not None:
        qs.append(f"remote_work={int(filters['remote'])}")
    if filters.get("max_age_days"):
        qs.append(f"count_days={int(filters['max_age_days'])}")
    if page > 1:
        qs.append(f"page_num={page}")
    return f"{BASE}{path}?{'&'.join(qs)}"


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
        salary_node = row.css_first("span.label-group .label")

        # data-dimension13="d4" → 4 days
        info = row.css_first("span.info")
        posted_days = -1
        if info:
            dim = info.attributes.get("data-dimension13") or ""
            m = re.search(r"\d+", dim)
            if m:
                posted_days = int(m.group())

        jobs.append(Job(
            id=offer_id,
            title=(title_node.text(strip=True) if title_node else a.text(strip=True)),
            url=urljoin(BASE, href),
            employer=employer_node.text(strip=True) if employer_node else "",
            location=loc_node.text(strip=True) if loc_node else "",
            posted=posted_node.text(strip=True) if posted_node else "",
            posted_days=posted_days,
            salary=salary_node.text(strip=True) if salary_node else "",
            matched_keyword=keyword,
        ))
    return jobs


def search(client: httpx.Client, keyword: str, max_pages: int, filters: dict) -> list[Job]:
    out: list[Job] = []
    for page in range(1, max_pages + 1):
        url = build_search_url(keyword, page, filters)
        html = fetch(client, url)
        page_jobs = parse_listing(html, keyword)
        if not page_jobs:
            break
        out.extend(page_jobs)
        time.sleep(0.0)  # stress-test: was 1.0; bump back up if 429s appear
    return out


def llm_score_batch(jobs: list[Job], cfg: dict, batch_size: int = 40) -> dict[str, int]:
    """Score jobs in batches via a single JSON-mode request each. Returns {job_id: score 0-10}."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("  [llm] OPENAI_API_KEY not set, skipping filter", file=sys.stderr)
        return {j.id: 10 for j in jobs}

    scores: dict[str, int] = {}
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i + batch_size]
        job_lines = "\n".join(
            f"{j.id} | {j.title} | {j.employer} | {j.location}" for j in batch
        )
        prompt = (
            f"Profile:\n{cfg['profile'].strip()}\n\n"
            f"Score each job below 0-10 for relevance to the profile.\n"
            f"Return a JSON object: {{\"<job_id>\": <score>, ...}} — no other text.\n\n"
            f"Jobs (id | title | employer | location):\n{job_lines}"
        )
        try:
            r = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": cfg["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
                timeout=60,
            )
            r.raise_for_status()
            data = json.loads(r.json()["choices"][0]["message"]["content"])
            for jid, score in data.items():
                try:
                    scores[str(jid)] = int(score)
                except (ValueError, TypeError):
                    scores[str(jid)] = 0
        except Exception as e:
            print(f"  [llm] batch {i}-{i+len(batch)} error: {e}", file=sys.stderr)
            for j in batch:
                scores[j.id] = 10  # fail open
    # Ensure every job has a score
    for j in jobs:
        scores.setdefault(j.id, 10)
    return scores


def notify_telegram(jobs: list[Job], dashboard_url: str | None) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, printing instead:", file=sys.stderr)
        for j in jobs:
            print(f"  - {j.title} @ {j.employer} ({j.location}) :: {j.url}")
        return

    for j in jobs:
        salary_line = f"\n💶 {esc(j.salary)}" if j.salary else ""
        text = (
            f"<b>{esc(j.title)}</b>\n"
            f"{esc(j.employer)}\n"
            f"{esc(j.location)}{salary_line}\n"
            f"<i>{esc(j.posted)}</i> · <code>{esc(j.matched_keyword)}</code>\n"
            f"{j.url}"
        )
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"telegram error {r.status_code}: {r.text}", file=sys.stderr)
        time.sleep(0.5)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    if LEGACY_STATE.exists():
        # Migrate old seen.json — keep IDs as a set, jobs list empty.
        old = json.loads(LEGACY_STATE.read_text())
        return {"jobs": [], "seen_ids": old.get("seen", [])}
    return {"jobs": [], "seen_ids": []}


def save_state(state: dict, max_jobs: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # newest first, capped
    state["jobs"] = state["jobs"][:max_jobs]
    state["seen_ids"] = state["seen_ids"][-5000:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def render_dashboard(jobs: list[Job], cfg: dict, generated_at: str) -> str:
    title = cfg.get("dashboard", {}).get("title", "AI Jobs")
    rows = []
    for j in jobs:
        salary = esc(j.salary) if j.salary else "—"
        rows.append(f"""
<article class="job" data-keyword="{esc(j.matched_keyword)}" data-days="{j.posted_days}">
  <h2><a href="{j.url}" target="_blank" rel="noopener">{esc(j.title)}</a></h2>
  <div class="meta">
    <span class="employer">{esc(j.employer)}</span>
    <span class="loc">{esc(j.location)}</span>
  </div>
  <div class="badges">
    <span class="badge salary">{salary}</span>
    <span class="badge age">{esc(j.posted) or '—'}</span>
    <span class="badge kw">{esc(j.matched_keyword)}</span>
  </div>
</article>""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
  :root {{
    --bg:#0d1117; --panel:#161b22; --border:#30363d;
    --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --green:#3fb950;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:var(--bg); color:var(--fg);
  }}
  header {{
    padding:1.5rem 2rem; border-bottom:1px solid var(--border);
    display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:1rem;
  }}
  h1 {{ margin:0; font-size:1.25rem; }}
  .stats {{ color:var(--muted); font-size:.85rem; }}
  .toolbar {{
    padding:1rem 2rem; display:flex; gap:.75rem; flex-wrap:wrap;
    background:var(--panel); border-bottom:1px solid var(--border);
  }}
  .toolbar input, .toolbar select {{
    background:var(--bg); color:var(--fg); border:1px solid var(--border);
    border-radius:6px; padding:.4rem .7rem; font-size:.9rem;
  }}
  .toolbar input {{ flex:1; min-width:200px; }}
  main {{ padding:1rem 2rem; max-width:1100px; margin:0 auto; }}
  .job {{
    background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:1rem 1.25rem; margin-bottom:.75rem;
    transition:border-color .15s;
  }}
  .job:hover {{ border-color:var(--accent); }}
  .job h2 {{ margin:0 0 .35rem; font-size:1rem; font-weight:600; }}
  .job h2 a {{ color:var(--fg); text-decoration:none; }}
  .job h2 a:hover {{ color:var(--accent); }}
  .meta {{ color:var(--muted); font-size:.85rem; margin-bottom:.5rem; }}
  .meta .employer {{ color:var(--fg); }}
  .meta .loc:before {{ content:" · "; }}
  .badges {{ display:flex; gap:.4rem; flex-wrap:wrap; }}
  .badge {{
    font-size:.75rem; padding:.15rem .55rem; border-radius:999px;
    border:1px solid var(--border); color:var(--muted);
  }}
  .badge.salary {{ color:var(--green); border-color:rgba(63,185,80,.4); }}
  .badge.kw {{ background:rgba(88,166,255,.1); color:var(--accent); }}
  .hidden {{ display:none !important; }}
  footer {{ padding:2rem; text-align:center; color:var(--muted); font-size:.8rem; }}
</style>
</head>
<body>
<header>
  <h1>{esc(title)}</h1>
  <div class="stats"><span id="visible">{len(jobs)}</span> / {len(jobs)} jobs · updated {esc(generated_at)}</div>
</header>
<div class="toolbar">
  <input id="q" type="search" placeholder="filter title / company / location…" autofocus>
  <select id="kw">
    <option value="">all keywords</option>
    {"".join(f'<option>{esc(k)}</option>' for k in sorted({j.matched_keyword for j in jobs}))}
  </select>
  <select id="age">
    <option value="">any age</option>
    <option value="1">≤ 1 day</option>
    <option value="3">≤ 3 days</option>
    <option value="7">≤ 7 days</option>
    <option value="14">≤ 14 days</option>
    <option value="31">≤ 31 days</option>
  </select>
</div>
<main id="list">
{"".join(rows)}
</main>
<footer>profesia-watch · static dashboard · <a href="https://github.com/Jozko25/profesia" style="color:var(--accent)">source</a></footer>
<script>
const q = document.getElementById('q');
const kw = document.getElementById('kw');
const age = document.getElementById('age');
const items = [...document.querySelectorAll('.job')];
const visible = document.getElementById('visible');

function apply() {{
  const qv = q.value.toLowerCase().trim();
  const kwv = kw.value;
  const agev = age.value ? parseInt(age.value) : null;
  let n = 0;
  for (const el of items) {{
    const txt = el.textContent.toLowerCase();
    const elKw = el.dataset.keyword;
    const elDays = parseInt(el.dataset.days);
    const matchQ = !qv || txt.includes(qv);
    const matchKw = !kwv || elKw === kwv;
    const matchAge = agev === null || (elDays >= 0 && elDays <= agev);
    const show = matchQ && matchKw && matchAge;
    el.classList.toggle('hidden', !show);
    if (show) n++;
  }}
  visible.textContent = n;
}}
[q, kw, age].forEach(el => el.addEventListener('input', apply));
</script>
</body>
</html>
"""


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yml").read_text())
    filters = cfg.get("filters") or {}
    max_jobs = cfg.get("dashboard", {}).get("max_jobs", 500)

    state = load_state()
    seen_ids = set(state["seen_ids"])
    stored_by_id = {j["id"]: j for j in state["jobs"]}

    exclude_terms = [t.lower() for t in (cfg.get("exclude_title_terms") or [])]

    all_jobs: dict[str, Job] = {}
    excluded = 0
    with httpx.Client(http2=False) as client:
        for kw in cfg["keywords"]:
            print(f"[search] {kw}")
            try:
                results = search(client, kw, cfg.get("max_pages", 2), filters)
            except Exception as e:
                print(f"  error: {e}", file=sys.stderr)
                continue
            for j in results:
                if j.id in all_jobs:
                    continue
                title_lower = j.title.lower()
                if any(term in title_lower for term in exclude_terms):
                    excluded += 1
                    continue
                all_jobs[j.id] = j
    if exclude_terms:
        print(f"[exclude] dropped {excluded} jobs by title term")

    new_jobs = [j for jid, j in all_jobs.items() if jid not in seen_ids]
    print(f"[stats] fetched={len(all_jobs)} new={len(new_jobs)} seen={len(seen_ids)}")

    # LLM filter (optional) — batched for speed + token efficiency
    llm_cfg = cfg.get("llm_filter", {})
    to_notify: list[Job] = []
    if llm_cfg.get("enabled") and new_jobs:
        min_score = llm_cfg.get("min_score", 6)
        print(f"[llm] scoring {len(new_jobs)} jobs in batches")
        scores = llm_score_batch(new_jobs, llm_cfg)
        for j in new_jobs:
            s = scores.get(j.id, 0)
            mark = "✓" if s >= min_score else "·"
            print(f"  {mark} [score={s}] {j.title}")
            if s >= min_score:
                to_notify.append(j)
    else:
        to_notify = new_jobs

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Stamp first_seen on newly inserted jobs; carry over old timestamp on re-encounters.
    for jid, j in all_jobs.items():
        if jid in stored_by_id:
            j.first_seen = stored_by_id[jid].get("first_seen", now_iso)
        else:
            j.first_seen = now_iso

    # Merge: new findings replace any stored copy (fresh posted text/salary)
    merged = {**stored_by_id, **{jid: asdict(j) for jid, j in all_jobs.items()}}
    sorted_jobs = sorted(
        merged.values(),
        key=lambda d: (d.get("posted_days", 999) if d.get("posted_days", -1) >= 0 else 999,
                       d.get("first_seen", "")),
    )

    state["jobs"] = sorted_jobs[:max_jobs]
    state["seen_ids"] = list(seen_ids | set(all_jobs.keys()))

    # Render dashboard from full store
    job_objs = [Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__})
                for d in state["jobs"]]
    DOCS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DOCS_FILE.write_text(render_dashboard(job_objs, cfg, now_iso))
    print(f"[dashboard] wrote {DOCS_FILE} ({len(job_objs)} jobs)")

    if to_notify:
        notify_telegram(to_notify, None)
    else:
        print("[notify] nothing new")

    save_state(state, max_jobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
