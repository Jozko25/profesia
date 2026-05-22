# profesia-watch

Polls profesia.sk for new AI / AI Engineering / AI automation jobs, pings Telegram on hits, and renders a live dashboard on GitHub Pages.

**Live dashboard:** https://jozko25.github.io/profesia/

## How it works

`scraper.py` runs N keyword searches on profesia.sk → parses listings → applies server-side filters (salary, region, remote, posting age) → drops jobs whose title matches an excluded term → optionally LLM-scores remaining jobs via OpenAI → notifies on truly new ones via Telegram. State (full job records) is persisted in `state/jobs.json`; the dashboard at `docs/index.html` is regenerated every run.

GitHub Actions cron runs the scraper every 2h and commits updates back to the repo.

## Setup

### 1. Telegram bot
1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → save token.
2. Start a chat with the bot, send any message.
3. Get `chat_id`: `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → look for `chat.id`.

### 2. Repo secrets
https://github.com/Jozko25/profesia/settings/secrets/actions

- `TELEGRAM_BOT_TOKEN` — required
- `TELEGRAM_CHAT_ID` — required
- `OPENAI_API_KEY` — required only if `llm_filter.enabled: true` in `config.yml`

### 3. Enable GitHub Pages (for dashboard)
https://github.com/Jozko25/profesia/settings/pages → Source: `Deploy from a branch` → Branch: `main` / `/docs` → Save.

### 4. Trigger first run
https://github.com/Jozko25/profesia/actions → "poll-profesia" → "Run workflow". Cron auto-runs every 2h after.

## Local run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
# export OPENAI_API_KEY=...   # only if llm_filter enabled
python scraper.py
```

Without Telegram env vars, jobs print to stdout — good for dry runs.

## Tuning (edit `config.yml`)

- **`keywords`** — search terms (one query per keyword, deduped by job ID)
- **`exclude_title_terms`** — drop jobs whose title contains any of these (EN + SK)
- **`filters.min_salary`** — minimum EUR/month (server-side filter on profesia.sk)
- **`filters.region`** — e.g. `bratislavsky-kraj`, or null for all Slovakia
- **`filters.remote`** — `0` on-site, `1` fully remote, `2` hybrid, null = any
- **`filters.max_age_days`** — 1/3/7/14/31, or null
- **`llm_filter.enabled`** — `true` to score with OpenAI (batched, ~1 call per run)
- **`llm_filter.profile`** — your role-fit description used as the scoring rubric
- **`dashboard.max_jobs`** — cap on dashboard size (default 500)

Cron frequency: `.github/workflows/poll.yml` → `schedule.cron`.

Reset state: empty `state/jobs.json` to `{"jobs": [], "seen_ids": []}` and re-run (will flood-notify ~200 jobs).
