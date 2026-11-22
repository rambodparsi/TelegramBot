# Implementation Plan — Personal AI Assistant (Version A)

Status: draft for review · 2026-11-22
Companion to the architecture blueprint (v1.0). This document turns that blueprint into
concrete, ordered work with exact versions, configs, schemas, and acceptance criteria.

**Locked decisions**

| Decision | Choice |
| --- | --- |
| Runtime | Cloudflare Python Worker (Pyodide), Workers **Free** plan as the target |
| Orchestration | LangGraph `StateGraph`, `graph.ainvoke()` only (never `invoke()`) |
| Checkpointer | `WorkerCloudflareD1Saver` from `langgraph-checkpoint-cloudflare-d1[worker]` |
| LLM | Gemini via a thin REST adapter (no `langchain-google-genai`) |
| Interface | Telegram Bot API webhooks |
| Async processing | Cloudflare Queues (one Worker with `fetch` + `queue` handlers) |
| Storage | One D1 database: checkpoint tables (owned by the saver) + app tables (owned by us) |

---

## 1. Research findings that change the plan

These were verified on 2026-11-22 by downloading the official package source
(`langgraph-checkpoint-cloudflare-d1==0.1.6`, including its `examples/workers/`).

1. **LangGraph does not install cleanly on Pyodide.** `langgraph` and its dependencies pull in
   four native extensions with no Pyodide wheels: `xxhash`, `ormsgpack`, `uuid_utils`,
   `websockets`. Cloudflare's example works around this with a script
   (`setup_pyodide_deps.sh`) that runs *after* `pywrangler sync`, manually extracts pinned
   pure-Python wheels into `python_modules/`, and drops in pure-Python stubs for those four.
   We must adopt the same build step.
2. **The official `ormsgpack` stub is broken for real chat state.** Cloudflare's example only
   checkpoints an `int`. When the state contains LangChain messages, the stub fails
   (`AttributeError: module 'ormsgpack' has no attribute 'MsgpackEncodeError'`), because it
   omits the exception classes and ignores the `default=` / `ext_hook=` hooks LangGraph's
   serializer relies on. **Reproduced locally.**
   A corrected shim (msgpack-backed, forwards `default` and `ext_hook`, defines the error
   classes) round-trips `HumanMessage` / `AIMessage` with tool calls correctly under
   CPython. **It still has to be proven inside Pyodide in Milestone 0.**
3. **Version set the official example is built and tested against** (we pin to exactly
   these first, and only upgrade deliberately):

   | Package | Version |
   | --- | --- |
   | langgraph | 1.2.11 |
   | langgraph-checkpoint | 4.2.0 |
   | langgraph-sdk | 0.4.2 |
   | langchain-core | 1.5.4 |
   | langchain-protocol | 0.0.18 |
   | langgraph-checkpoint-cloudflare-d1 | 0.1.6 (`[worker]` extra) |
   | sqlalchemy-cloudflare-d1 | ≥ 0.3.1 |
   | workers-py (pywrangler) | ≥ 1.16.4, < 2 |
   | wrangler (npm) | ^4.58 |

   Note: PyPI currently has newer releases (langgraph 1.2.24, langchain-core 1.8.4,
   langgraph-checkpoint 4.3.2). Do not take them until the pinned set works end to end.
4. **Python Workers are still labelled beta** in Cloudflare's example. Expect API drift;
   pinning plus a smoke test in CI is mandatory.
5. **`langgraph.prebuilt` (`ToolNode`) is not extracted by the example script.** We will
   write our own small tool-execution node (blueprint §9.3, option 2) rather than add
   another package to the Pyodide build.

---

## 2. What you need to provide

| Item | How to get it | Where it goes |
| --- | --- | --- |
| Cloudflare account (Free) | dash.cloudflare.com | — |
| Cloudflare API token (deploy) | My Profile → API Tokens → "Edit Cloudflare Workers" template, plus D1 Edit and Queues Edit | GitHub Actions secret `CLOUDFLARE_API_TOKEN`; local `wrangler login` works too |
| Cloudflare account ID | Dashboard sidebar | GitHub Actions secret `CLOUDFLARE_ACCOUNT_ID` |
| Telegram bot token (prod) | @BotFather → `/newbot` | `wrangler secret put TELEGRAM_BOT_TOKEN` |
| Telegram bot token (test) — recommended | second bot via @BotFather | separate Worker environment |
| Your Telegram user ID | e.g. @userinfobot | `wrangler secret put AUTHORIZED_TELEGRAM_USER_IDS` |
| Webhook secret | random 32+ chars (`openssl rand -hex 32`) | `wrangler secret put TELEGRAM_WEBHOOK_SECRET` |
| Gemini API key | aistudio.google.com → API keys | `wrangler secret put GEMINI_API_KEY` |
| Gemini model ID | pick from live `models.list` output in M1 | `wrangler.jsonc` `vars.GEMINI_MODEL` (not secret) |

**Decision needed before real personal data:** Gemini free tier vs. paid tier (free-tier
prompts may be used by Google to improve products). Development uses synthetic data only.

---

## 3. Repository layout (built up progressively)

```
TelegramBot/
  wrangler.jsonc               # Worker, D1, Queue, cron, vars
  pyproject.toml               # pinned deps; dev group: workers-py, pytest
  package.json                 # wrangler pinned
  scripts/
    setup_pyodide_deps.sh      # extract pinned wheels + copy shims (from official example, adapted)
    set_webhook.py             # register/inspect Telegram webhook
  shims/                       # pure-Python replacements for native deps
    ormsgpack/  xxhash/  uuid_utils/  websockets/
  src/
    entry.py                   # WorkerEntrypoint: fetch(), queue(), scheduled()
    config.py                  # typed access to env vars/secrets
    telegram/  webhook.py  client.py  parser.py  commands.py
    graph/     state.py  builder.py  nodes.py  routing.py
    llm/       gemini.py  convert.py          # LangChain msgs <-> Gemini contents
    tools/     registry.py  executor.py  builtin.py
    memory/    context.py  preferences.py
    media/     download.py
    storage/   db.py  updates.py  conversations.py  messages.py
    delivery/  send.py  format.py
  prompts/main_assistant.md
  migrations/0001_app_schema.sql
  benchmarks/                  # M0 probe endpoints + results
  tests/unit/  tests/integration/  tests/fixtures/
  .github/workflows/ci.yml  deploy.yml
  docs/IMPLEMENTATION_PLAN.md
```

Rule: `graph/` never imports `telegram/`; `telegram/` never touches checkpoints;
`storage/` never touches checkpoint tables.

---

## 4. Milestones

Each milestone ends deployed and verified on Cloudflare, not just locally.

### Milestone 0 — Feasibility spike (the go/no-go)

Goal: prove the pinned LangGraph stack runs in a Python Worker on the Free plan with
**message-based state**, and measure its cost.

Tasks
1. Scaffold with `uvx --from workers-py pywrangler init`; adapt `pyproject.toml`,
   `package.json`, `wrangler.jsonc` from the official example.
2. Add `scripts/setup_pyodide_deps.sh` (pinned versions above) and `shims/` — using **our
   corrected `ormsgpack` shim**, not the official one.
3. `wrangler d1 create assistant-db`; bind as `DB`.
4. Probe endpoints (temporary, protected by a `BENCH_TOKEN` secret header):
   - `/bench/noop` — baseline Worker cost.
   - `/bench/import` — import langgraph + langchain_core only.
   - `/bench/graph-int` — official int graph with D1 saver (reproduces Cloudflare's example).
   - `/bench/graph-messages` — 3-node graph whose state is `messages` with a fake
     `AIMessage` containing a tool call, a fake tool node, and a loop back. No network.
   - `/bench/graph-resume?thread=X` — second call on the same thread; asserts history restored.
   - `/bench/gemini` — same as above but `agent` node calls Gemini for real.
5. Record per endpoint: CPU time (Workers observability), wall time, memory, D1 rows
   read/written, checkpoint blob size. Run each ≥ 20 times; record p50/p95 and the
   error rate (including "exceeded CPU" errors). Results go in `benchmarks/RESULTS.md`.
6. Repeat `/bench/graph-messages` with a 40-message thread to see growth.

Go/no-go
- **Go:** `graph-messages` and `gemini` succeed ≥ 99% over 20+ runs with no CPU-limit
  errors, and resume works across invocations.
- **No-go options (decide then, not now):** (a) Workers Paid ($5/mo, CPU limit removed in
  practice), (b) reduce graph overhead (fewer super-steps, `durability="exit"`, trimmed
  state), (c) move the graph executor to another host while keeping Telegram/D1 on Cloudflare.

Deliverable: `benchmarks/RESULTS.md` with numbers and a written go/no-go.

### Milestone 1 — Telegram + Gemini text, synchronous path

Keep it simple: webhook → graph → reply inside the `fetch` handler, protected by the
secret and allowlist. Queue comes in M5. (If M0 shows Gemini latency risks Telegram's
webhook timeout, pull the queue forward.)

Tasks
1. `config.py` — read env; fail fast on missing secrets.
2. `telegram/webhook.py` — verify `X-Telegram-Bot-Api-Secret-Token` (constant-time
   compare), parse JSON, accept only `message` updates in private chats from allowlisted
   user IDs; everything else → `200 OK` and drop (never reveal the bot to strangers).
3. `telegram/parser.py` — normalize to `InboundMessage{update_id, chat_id, user_id,
   message_id, kind: text|voice|photo|command, text, caption, file_id, mime, size}`.
4. Dedup on `update_id` (table `telegram_updates`, `INSERT ... ON CONFLICT DO NOTHING`).
5. `llm/gemini.py` — `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`,
   header `x-goog-api-key`. Map errors: 429/5xx → retryable, 400/403 → permanent.
   Parse `usageMetadata` for token counts.
6. `llm/convert.py` — LangChain messages ⇄ Gemini `contents`
   (`HumanMessage`→`role:user`, `AIMessage`→`role:model`, `SystemMessage`→`systemInstruction`).
7. Graph v1: `START → agent → finalize → END`.
8. `delivery/` — `sendChatAction(typing)` before the model call; `sendMessage` with
   `parse_mode=HTML`; convert model Markdown to Telegram-safe HTML (bold, italic, code,
   pre, links only; escape `& < >`); split at 4096 chars on paragraph boundaries; on a
   400 "can't parse entities" error, resend as plain text.
9. Commands: `/start`, `/help`, `/new` (new conversation → new `thread_id`).
10. `scripts/set_webhook.py` — `setWebhook` with `secret_token`,
    `allowed_updates=["message","callback_query"]`, `drop_pending_updates` option;
    `getWebhookInfo` check.

Done when: deployed bot answers text, ignores other users, survives a duplicated update.

### Milestone 2 — Voice and photos

1. `media/download.py` — `getFile` → download from
   `https://api.telegram.org/file/bot<token>/<file_path>`. Enforce limits **before**
   downloading using the `file_size` in the update (Bot API download limit is 20 MB; we set
   our own lower cap, e.g. 10 MB, and tune after M0 memory numbers).
2. Allowed types: photo (JPEG from Telegram), `image/png`/`image/webp` documents, voice
   (`audio/ogg`, Opus). Anything else → friendly "unsupported" reply.
3. Send media to Gemini as `inline_data {mime_type, data: base64}`. Measure CPU of the
   base64 step in Pyodide — this is the most likely CPU hotspot.
4. Voice: one Gemini call to transcribe → transcript becomes the user's text turn
   (stored as `HumanMessage` with `additional_kwargs.source="voice"`). Empty/unclear
   transcript → ask the user to repeat; never invent content.
5. Photo: image + caption go into the current turn only. **Bytes never enter graph state**;
   state gets a text marker (`[photo: <file_unique_id>]`) and the model's interpretation
   becomes part of the answer, so follow-up questions still work.

Done when: text, voice, photo, photo+caption, oversized, and unsupported all pass E2E.

### Milestone 3 — Memory

1. Thread IDs: `telegram:{chat_id}:{conversation_id}`; `conversations` table holds the
   active one per chat; `/new` creates another.
2. Checkpointer wired in for real (`WorkerCloudflareD1Saver(env.DB)`, `await saver.setup()`
   once per isolate, cached in a module global).
3. Context window: `memory/context.py` sends the system prompt + preferences + the last
   N turns (start N=20, cap by an approximate token budget) to Gemini. Full state stays
   in the checkpoint; only the trimmed view goes to the model.
4. Checkpoint growth control: trim `messages` in state to the last ~60 using
   `RemoveMessage` once a threshold is crossed; full history lives in our `messages` table.
5. Application `messages` table written in `finalize` (user turn + assistant turn).
6. `/reset` = new thread + mark old conversation closed. `/deletehistory` = delete
   our rows **and** `saver.adelete_thread(thread_id)` after inline-keyboard confirmation.

Done when: context survives a redeploy; `/new` isolates; delete removes checkpoints too.

### Milestone 4 — Tools

1. `tools/registry.py` — each tool: name, description, JSON-schema params, risk level
   (`read` / `write`), Python handler. Gemini gets `tools: [{functionDeclarations: [...]}]`.
2. Initial tools: `get_current_datetime(timezone?)`, `get_user_preferences(keys?)`,
   `save_user_preference(key, value)` (allowlisted keys: `timezone`, `language`,
   `answer_style`, `currency`, `name`).
3. Graph v2: `START → load_context → agent → (route) → tools → agent … → finalize → END`.
4. `graph/routing.py`: tool calls present and `tool_call_count < MAX_TOOL_CALLS` (3) →
   `tools`; budget exceeded → `finalize` with an explanation; else → `finalize`.
   `tool_call_count` reset to 0 at the start of every user turn.
5. `tools/executor.py` — unknown tool → error result to model; validate args against
   schema (hand-rolled validator, no extra deps); execute; return
   `ToolMessage(tool_call_id=…)`; log a sanitized row in `tool_executions`.
6. Gemini function-call mapping: `functionCall{name,args}` ⇄ `AIMessage.tool_calls`
   (generate our own stable IDs), `ToolMessage` ⇄ `functionResponse{name,response}`.
   Preserve any `thoughtSignature` parts Gemini returns, by storing them in
   `AIMessage.additional_kwargs` and sending them back unchanged.

Done when: "remember I prefer short answers" writes D1, then later answers are short; the
bot never says "saved" unless the write succeeded.

### Milestone 5 — Reliability and production

Scaled to a single user (the blueprint's full ledger/outbox is deferred to the Finance agent):
1. Queue `assistant-jobs` (+ dead-letter `assistant-jobs-dlq`). `fetch` verifies →
   dedups → `env.MESSAGE_QUEUE.send(job)` → `200`. `queue()` handler runs the graph.
2. Consumer settings: `max_batch_size: 1`, `max_concurrency: 1`, `max_retries: 3`.
   Concurrency 1 gives per-chat ordering for one user.
3. `telegram_updates.status`: `received → queued → done | failed`. Duplicate update with
   status `received` (enqueue may have failed) → re-enqueue; otherwise ignore.
4. Cron every 10 min: re-enqueue `received` rows older than 2 min; prune old rows.
5. Errors: retryable (Gemini 429/5xx, network) → `message.retry()`; permanent → reply
   "Sorry, something went wrong (ref XXXX)" and `ack()`.
6. Outbound: store `response_id` before sending; on retry, skip sending if that turn's
   response is already marked sent. Accept a rare duplicate for chat.
7. CI/CD (below), secrets audit, usage-event logging, `/status` command.

Done when: forced failures (Gemini 500, killed mid-run, duplicate update) all recover.

### Milestone 6 — Specialized agents (later, separate plan)

Finance first, as a subgraph with `interrupt()` approvals and strict idempotency
(this is where the full ledger/outbox from the blueprint gets built).

---

## 5. Graph specification

```python
class AssistantState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    current_input: dict          # normalized InboundMessage (no bytes)
    user_preferences: dict       # loaded by load_context
    tool_call_count: int
    final_response: str | None
    error: str | None
```

| Node | Does | Writes |
| --- | --- | --- |
| `load_context` | load prefs from D1; reset `tool_call_count` | `user_preferences`, `tool_call_count=0` |
| `agent` | build trimmed context, call Gemini with tool schemas | `messages += [AIMessage]` |
| `tools` | validate + run each tool call | `messages += [ToolMessage…]`, `tool_call_count` |
| `finalize` | pick final text, persist app `messages` rows, trim state | `final_response` |

Media download/transcription happens **before** `ainvoke` (in the queue handler), so the
graph receives only text plus, for photos, a transient image reference passed through
`config["configurable"]` rather than state.

Invocation: `await graph.ainvoke({"messages": [HumanMessage(...)], "current_input": …},
config={"configurable": {"thread_id": tid}}, durability=<from M0 results>)`.

---

## 6. D1 application schema (`migrations/0001_app_schema.sql`)

```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  telegram_user_id INTEGER NOT NULL UNIQUE,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE conversations (
  id TEXT PRIMARY KEY,                       -- uuid
  user_id INTEGER NOT NULL REFERENCES users(id),
  chat_id INTEGER NOT NULL,
  thread_id TEXT NOT NULL UNIQUE,            -- telegram:{chat_id}:{id}
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_conv_active ON conversations(chat_id, is_active);

CREATE TABLE telegram_updates (
  update_id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  status TEXT NOT NULL,                      -- received|queued|done|failed
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  received_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_updates_status ON telegram_updates(status, received_at);

CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  update_id INTEGER,
  role TEXT NOT NULL,                        -- user|assistant
  kind TEXT NOT NULL,                        -- text|voice|photo|command
  content TEXT NOT NULL,
  telegram_message_id INTEGER,
  sent_status TEXT,                          -- assistant rows: pending|sent|failed
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE user_preferences (
  user_id INTEGER NOT NULL REFERENCES users(id),
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT NOT NULL,                      -- tool|command
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, key)
);

CREATE TABLE tool_executions (
  id INTEGER PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  args_hash TEXT NOT NULL,
  status TEXT NOT NULL,                      -- ok|error|rejected
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE usage_events (
  id INTEGER PRIMARY KEY,
  update_id INTEGER,
  model TEXT, input_tokens INTEGER, output_tokens INTEGER,
  latency_ms INTEGER, tool_calls INTEGER, error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Checkpoint tables are created by `saver.setup()`; we never alter them. Migrations are
applied with `wrangler d1 migrations apply assistant-db --remote`, manually, never from CI
automatically.

---

## 7. `wrangler.jsonc` (target shape by M5)

```jsonc
{
  "name": "personal-assistant",
  "main": "src/entry.py",
  "compatibility_date": "2025-11-02",          // bump only with a test run
  "compatibility_flags": ["python_workers"],
  "vars": { "GEMINI_MODEL": "<chosen in M1>", "MAX_TOOL_CALLS": "3", "MAX_MEDIA_MB": "10" },
  "d1_databases": [{ "binding": "DB", "database_name": "assistant-db", "database_id": "<id>" }],
  "queues": {
    "producers": [{ "binding": "MESSAGE_QUEUE", "queue": "assistant-jobs" }],
    "consumers": [{ "queue": "assistant-jobs", "max_batch_size": 1, "max_concurrency": 1,
                    "max_retries": 3, "dead_letter_queue": "assistant-jobs-dlq" }]
  },
  "triggers": { "crons": ["*/10 * * * *"] },
  "observability": { "enabled": true }
}
```

Secrets (never in this file): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
`AUTHORIZED_TELEGRAM_USER_IDS`, `GEMINI_API_KEY`, `BENCH_TOKEN` (M0 only).

---

## 8. Testing

| Level | What | Where |
| --- | --- | --- |
| Unit (CPython, pytest) | parser, auth checks, HTML formatter + splitter, Gemini converter, tool validator, routing | CI on every push |
| Graph integration (CPython) | full graph with `InMemorySaver`, fake Gemini, fake tools: loop limits, thread isolation, resume | CI |
| Shim tests (CPython) | our `ormsgpack`/`xxhash`/`uuid_utils` shims round-trip real checkpoint payloads vs. the real libraries | CI |
| Worker integration | `pywrangler dev` + local D1: webhook → reply with Gemini/Telegram mocked via a fake HTTP server | CI (later M1) |
| E2E | test bot on deployed `staging` Worker, real Gemini | manual checklist per milestone |

Keep business logic in plain Python modules that do not import `workers`/`js`, so most
tests run under normal CPython.

## 9. CI/CD

- `ci.yml` (push/PR): `uv sync` → ruff → pytest → `pywrangler sync && setup_pyodide_deps.sh`
  (catches dependency breakage) → `wrangler deploy --dry-run`.
- `deploy.yml` (push to `main`, manual approval via GitHub environment): build → deploy to
  `staging` → smoke test → deploy to production → `getWebhookInfo` check.
- GitHub holds only `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.

## 10. Risks to watch

| Risk | Mitigation |
| --- | --- |
| CPU limit on Free plan | M0 numbers decide; fallbacks listed in M0 |
| Shim correctness (serializer) | dedicated shim tests against real libs; M0 resume test in Pyodide |
| Pyodide/package drift | exact pins, dependency build in CI |
| Checkpoint growth / D1 row size | trim state in `finalize`; measure blob size in M0 |
| base64 of media is CPU-heavy | measure in M2; lower size cap or downscale request via Telegram's smaller photo sizes |
| Gemini free-tier rate limits | retry with backoff via queue; friendly "busy" message |
| Gemini free-tier data use | synthetic data until the tier decision is made |

## 11. Open questions for you

1. Gemini tier: stay on free during development (synthetic data only) — OK?
2. Do you want a separate **test bot + staging Worker** from the start? (Recommended.)
3. Your timezone and the bot's default reply language.
4. How do you want to deploy during M0: run `pywrangler deploy` yourself, or add a
   Cloudflare API token to this cloud environment so I can deploy and benchmark directly?
