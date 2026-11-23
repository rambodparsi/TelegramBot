# TelegramBot — personal AI assistant

A private, multimodal Telegram assistant: **Python + LangGraph + Gemini on Cloudflare Workers**
(D1 for storage and LangGraph checkpoints, Queues for reliable processing).

- Text, voice notes and audio files (transcribed), photos
- English and Farsi
- Conversation memory (LangGraph checkpoints in D1), explicit long-term memory and preferences
- Tools: current time, preferences, memories — validated and audited
- Commands: `/new`, `/memory`, `/forget [id]`, `/deletehistory`, `/status`, `/whoami`, `/help`

Design and roadmap: [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) ·
Feasibility numbers: [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md)

## Environments

| | Worker | D1 | Queue | Bot |
|---|---|---|---|---|
| production | `tg-assistant` | `tg-assistant-prod` | `tg-assistant-jobs` (+ `-dlq`) | the main bot |
| staging | `tg-assistant-staging` | `tg-assistant-staging` | `tg-assistant-staging-jobs` (+ `-dlq`) | none (add a test bot's token to re-enable) |

Secrets per Worker (set in Cloudflare → Workers & Pages → Worker → Settings → Variables and Secrets):
`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `AUTHORIZED_TELEGRAM_USER_IDS`, `TELEGRAM_WEBHOOK_SECRET`,
`ADMIN_TOKEN` (staging uses `BENCH_TOKEN`). Model chains and limits are plain vars in `wrangler.jsonc`.

> A Telegram bot has one webhook. Registering a Worker's webhook (`POST /admin/setup-webhook`
> with `x-admin-token`) moves the bot to that Worker.

## Develop

```bash
# unit tests (CPython with the same pinned LangGraph stack the Worker bundles)
uv run --isolated --no-project --with-requirements requirements-test.txt pytest -q

# build the Pyodide bundle (pinned wheels + shims, see scripts/setup_pyodide_deps.sh)
npm ci && uv sync && uv run pywrangler sync && bash scripts/setup_pyodide_deps.sh
```

## Deploy

GitHub → **Actions → Deploy → Run workflow**, choose `staging` or `production`
(applies D1 migrations, deploys, health-checks). Needs repository secrets
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.
