# profesia-watch

Polls profesia.sk for new AI Engineering jobs, pings Telegram on hits.

## How it works
- `scraper.py` runs N keyword searches on profesia.sk, parses listings, dedups against `state/seen.json`, optionally LLM-filters, sends new jobs to Telegram.
- GitHub Actions cron runs it every 2h and commits the updated state file back.

## Setup

### 1. Telegram bot
1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → save token.
2. Start a chat with your new bot, send any message.
3. Get your chat_id: `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → look for `chat.id`.

### 2. Repo secrets
Push to GitHub, then in repo settings → Secrets → Actions:
- `TELEGRAM_BOT_TOKEN` — required
- `TELEGRAM_CHAT_ID` — required
- `OPENAI_API_KEY` — optional (only if `llm_filter.enabled: true` in config.yml)

### 3. Enable Actions
Push, then the cron starts. Manual trigger via Actions tab → "poll-profesia" → "Run workflow".

## Local run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
# export OPENAI_API_KEY=...   # optional
python scraper.py
```

Without Telegram env vars, jobs print to stdout. Good for dry runs.

## Tuning
- Keywords / page count / LLM filter: edit `config.yml`.
- Cron frequency: `.github/workflows/poll.yml` → `schedule.cron`.
- Reset state: empty `state/seen.json` → `{"seen": []}` and re-run (will spam-notify last ~200 jobs).
