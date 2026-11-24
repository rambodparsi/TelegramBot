# Planner Module: Implementation Specification

**Target repository:** `rambodparsi/TelegramBot` (Python + LangGraph + Gemini on Cloudflare Python Workers, D1, Queues)
**Feature:** let the Telegram assistant read and edit the owner's **Life Planner** Google Sheet through validated tools.
**Audience:** an AI coding agent (or developer) adding this feature to the repo.
**Status:** design approved; not yet implemented.

---

## 0. How to use this document

1. Read the whole document before writing code. Sections 3–6 describe the external system (the Google Sheet). Sections 7–17 describe the code to write.
2. Implement in the **phases in section 19**. Finish each phase's acceptance checks before starting the next. Each phase should be one reviewable commit or PR.
3. Follow the repo's existing conventions exactly:
   - Python 3.12, `from __future__ import annotations`, type hints, small modules, docstrings on public functions.
   - **No new third-party dependencies.** Everything here works with the standard library, the Workers runtime (`js`, `pyodide.ffi`) and what the repo already bundles. Do **not** add `google-auth`, `google-api-python-client`, `cryptography`, `requests`-based Google clients, or `pydantic` models.
   - Keep Worker CPU low (see `benchmarks/RESULTS.md`): no heavy imports, no per-request parsing of things that can be cached, one Sheets write call per message.
   - Imports used only inside the Worker (`js`, `pyodide`, `workers`) must be **lazy** (inside functions), the way `WorkerHttp` does it, so tests run on CPython.
   - Tests use the existing fakes pattern (`tests/fakes.py`, scripted Gemini responses as in `tests/unit/test_tools.py`). Run them with the command from `README.md`.
4. Where this document says **MUST**, it is a requirement. **SHOULD** is a strong default. **MAY** is optional.
5. Also add `docs/LIFE_PLANNER_AGENT_GUIDE.md` (provided separately by the owner) to the repo. It is the human-readable description of the sheet. This spec is the implementation contract; if they disagree, this spec wins for code behaviour.

---

## 1. Goal

The owner keeps their whole life plan in a Google Sheet: time blocks, to-dos, events, routines, habits, goals and reviews. They want to manage it by talking to their existing Telegram bot in English or Farsi, by text or voice, for example:

- "What's on today?" / "What does my week look like?"
- "Add 'call the bank' this week, it's important."
- "Put the Q4 report in tomorrow morning for two hours."
- "Move gym to Thursday after work." / "Cancel tonight's reading."
- "I finished the slides." / "I did my workout and meditation today."
- "Undo that."

The bot must make these changes **correctly and safely**:
- never corrupt the sheet;
- never create duplicates on retries;
- never move "Fixed" items without explicit confirmation;
- always log what it changed;
- always offer a way to undo.

### Non-goals (v1)

- No Google Calendar sync. Leave the `CalendarEventID` column untouched.
- No multi-user support. The planner belongs to the single owner (the first ID in `AUTHORIZED_TELEGRAM_USER_IDS`).
- No editing of view tabs, `Settings`, `README_AI` or `_GoalCalc`.
- No subgraph or `interrupt()`. Planner tools live in the existing agent loop (see section 7.3).

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **Sheet** | The owner's Google Sheets spreadsheet (one file, many tabs). |
| **Tab** | One worksheet inside the sheet, e.g. `Tasks`. |
| **Data tab** | A tab the bot may write to: `Inbox`, `Tasks`, `Schedule`, `Goals`, `Routines`, `Habits`, `Reviews`, `ChangeLog`. |
| **View tab** | A formula-only tab the bot MUST NOT write to: `Dashboard`, `Daily`, `Weekly`, `Monthly`, `Yearly`, `_GoalCalc` (hidden). |
| **Item** | One row in a data tab, identified by its ID (`T-0012`, `E-0040`, …). |
| **Op** | One planner tool call that changes the sheet. |
| **Op key** | Unique key for an op: `"{update_id}:{tool_name}:{args_hash}"`. Used for retry safety and undo. |
| **Fixed / Flexible** | Value of the `Flexibility` column. Fixed items are protected. |
| **Serial date** | Google Sheets' numeric date: days since 1899-12-30. The fractional part is the time of day. |

---

## 3. The Google Sheet: structure

The sheet was created from `Life_Planner.xlsx` and imported into Google Sheets (File → Import → Replace spreadsheet). It is a native Google Sheet. **Tab names and column orders below are a hard contract.**

Row 1 of every data tab is a header. Data starts at row 2. The bot never inserts rows in the middle, never deletes rows, never sorts, never renames anything.

### 3.1 `Tasks`: to-dos (rows 2–1000)

| Col | Header | Type | Required | Notes |
|---|---|---|---|---|
| A | TaskID | `T-0001` | yes | `T-` + 4 digits, unique |
| B | Title | text | yes | ≤ 120 chars |
| C | Area | enum Areas | yes | |
| D | GoalID | `G-###` | no | must exist in Goals if set |
| E | Priority | `P1`–`P4` | yes | default `P2` |
| F | Status | enum TaskStatus | yes | new → `Not started` |
| G | DueDate | date | no | deadline |
| H | PlannedDate | date | no | day the owner intends to do it |
| I | EstMinutes | integer | no | 5–720 |
| J | Flexibility | `Fixed`/`Flexible` | yes | default `Flexible` |
| K | Notes | text | no | ≤ 500 chars |
| L | CreatedBy | enum Actor | yes | bot writes `ai` |
| M | CreatedAt | date-time | yes | |
| N | UpdatedAt | date-time | yes | |
| O | CompletedDate | date | when Done | |

### 3.2 `Schedule`: time blocks and events (rows 2–3000)

| Col | Header | Type | Required | Notes |
|---|---|---|---|---|
| A | EventID | `E-0001` | yes | `E-` + 4 digits |
| B | Date | date | yes | |
| C | Start | time | yes | |
| D | End | time | yes | > Start; `24:00` allowed (serial 1.0) |
| E | Title | text | yes | ≤ 120 chars |
| F | Type | enum EventType | yes | `Event`, `Task block`, `Routine`, `Focus`, `Break`, `Travel` |
| G | Area | enum Areas | yes | |
| H | LinkedID | `T-####` or `R-##` | for task blocks/routines | |
| I | Flexibility | `Fixed`/`Flexible` | yes | |
| J | Status | enum EventStatus | yes | `Planned`, `Done`, `Skipped`, `Cancelled` |
| K | Location | text | no | |
| L | Notes | text | no | |
| M | CalendarEventID | text | no | **never write in v1** |
| N | CreatedBy | enum Actor | yes | |
| O | UpdatedAt | date-time | yes | |

A block never crosses midnight. If one must, it is stored as two rows.

### 3.3 `Goals` (rows 2–301)

| Col | Header | Type | Notes |
|---|---|---|---|
| A | GoalID | `G-001` | `G-` + 3 digits |
| B | Title | text | |
| C | Level | `Year`/`Quarter`/`Month`/`Week` | |
| D | Area | enum Areas | |
| E | ParentGoalID | `G-###` | optional |
| F | StartDate | date | |
| G | EndDate | date | Year views use `YEAR(EndDate)` |
| H | Metric | text | e.g. `books` |
| I | Target | number | optional |
| J | Current | number | optional |
| K | Status | `Active`/`Done`/`Paused`/`Dropped` | |
| L | Why / Notes | text | |
| M | CreatedBy | enum Actor | |
| N | UpdatedAt | date-time | |

Goal **progress** is computed by the sheet in hidden tab `_GoalCalc`. Row *n* of `_GoalCalc` corresponds to row *n* of `Goals`: column A = GoalID, column I = progress (0–1, or empty). The bot reads it and never computes progress itself.

### 3.4 `Routines` (rows 2–100)

| Col | Header | Notes |
|---|---|---|
| A | RoutineID | `R-` + 2 digits |
| B | Title | |
| C | Area | |
| D | Days | `Daily`, `Weekdays`, `Weekends`, or a comma list of `Mon,Tue,Wed,Thu,Fri,Sat,Sun` |
| E | WindowStart | time |
| F | WindowEnd | time |
| G | DurationMin | integer |
| H | Flexibility | Fixed → always starts at WindowStart; Flexible → anywhere in the window |
| I | Type | usually `Routine`, `Focus` or `Break` |
| J | Active | `Yes`/`No` |
| K | Notes | |
| L | UpdatedAt | |

Routines are templates. They appear in views only once copied into `Schedule` as dated rows with `LinkedID = RoutineID`.

### 3.5 `Habits` (pre-filled rows 2–427)

- Column A = Date (one row per day, **2026-11-01 → 2027-12-31**), B = weekday text, C–J = one habit per column, K = Notes.
- Row 1, C1:J1, holds formulas showing the habit names from `Settings!N2:N9`. An unused slot shows `(unused)`. Read these computed values to map habit name → column.
- Cell values are `TRUE`, `FALSE` or empty.
- The bot updates cells by (date row, habit column). It MUST NOT append rows. If a date is outside the pre-filled range, return error `out_of_range`.

### 3.6 `Inbox` (rows 2–1000)

| A InboxID (`I-0001`) | B CapturedAt | C Text | D Status (`New`/`Processed`/`Discarded`) | E ConvertedToID | F Notes | G CreatedBy |

### 3.7 `Reviews` (rows 2–1000)

| A ReviewID (`RV-0001`) | B Type (`Daily`/`Weekly`/`Monthly`/`Yearly`) | C PeriodStart | D Wins | E Challenges | F Lessons | G NextFocus | H Energy1to5 | I Mood1to5 | J CreatedBy | K CreatedAt |

### 3.8 `ChangeLog` (rows 2–5000): **one column is added by this project**

| Col | Header | Notes |
|---|---|---|
| A | Timestamp | date-time |
| B | Actor | `ai` for the bot |
| C | Action | `create`, `update`, `move`, `complete`, `cancel`, `undo` |
| D | Tab | e.g. `Tasks` |
| E | ItemID | e.g. `T-0012` |
| F | Field | column header name; empty for `create` |
| G | OldValue | as displayed text |
| H | NewValue | as displayed text |
| I | Reason | short human reason |
| **J** | **RequestID** | **NEW.** The op key (section 11). Empty for human edits. |

**One-time sheet change (manual, by the owner):** type `RequestID` into `ChangeLog!J1`. The bot MUST check at startup of a write (cached per message) that `ChangeLog!J1 == "RequestID"`. If it doesn't match, refuse all writes with error `sheet_not_ready` and a message telling the owner to add the header.

### 3.9 `Settings`: read-only for the bot

Key/value cells (column B):

| Cell | Key | Default | Use |
|---|---|---|---|
| B2 | PlanningYear | 2027 | goals listing default |
| B3 | Timezone | `America/Chicago` | fallback timezone |
| B4 | WeekStart | `Monday` | weeks are Mon–Sun |
| B5 | DayStart | 06:00 (serial 0.25) | earliest schedulable time |
| B6 | DayEnd | 24:00 (serial 1.0) | latest schedulable end |
| B7 | SlotMinutes | 30 | rounding granularity hint |
| B8 | WorkHours | `09:00-17:00` | text range |
| B9 | MaxFocusHoursPerDay | 4 | cap on Focus + Task block hours |
| B10 | BufferMinutes | 10 | gap around blocks the bot creates |
| B11 | QuietHours | `23:00-06:00` | text range, may wrap midnight |
| B12 | AIMayMove | `Flexible` | informational |
| B13 | AIMayDelete | `No` | informational |

Allowed-value lists (header in row 1, values from row 2 down, stop at the first empty cell):

| Column | List |
|---|---|
| E | Areas (default: Work, Health, Money, Family, Personal growth, Home, Fun) |
| G | TaskStatus |
| H | EventStatus |
| I | Priority |
| J | EventType |
| K | Flexibility |
| L | GoalLevel |
| M | GoalStatus |
| N | Habits (N2:N9, may contain blanks; skip blanks) |
| O | ReviewType |
| P | Actor |
| Q | InboxStatus |
| R | YesNo |

Read the ranges `Settings!A2:B13` and `Settings!E1:R12` in one `batchGet`.

---

## 4. Value formats

### 4.1 Reading

Always read with `valueRenderOption=UNFORMATTED_VALUE` and `dateTimeRenderOption=SERIAL_NUMBER`.

- **Dates** arrive as numbers (serial). Convert: `date(1899, 12, 30) + timedelta(days=int(v))`.
- **Times** arrive as fractions of a day. Convert to minutes: `round(v * 1440)`. `1440` means `24:00`.
- **Date-times:** integer part = date, fraction = time.
- **Booleans** arrive as `true`/`false`.
- **Text that looks like a date** (a human typed `'2026-11-24` as text): accept ISO `YYYY-MM-DD` strings too. Anything else is treated as empty and logged at warning level.
- **The API omits trailing empty cells and trailing empty rows.** Pad each row to the expected width with `None`.

### 4.2 Writing

Always write with `valueInputOption=USER_ENTERED` and these string formats, so Google Sheets stores real dates and times:

| Kind | Write as | Example |
|---|---|---|
| Date | `YYYY-MM-DD` | `2026-11-24` |
| Time | `HH:MM` | `07:30`, `24:00` |
| Date-time | `YYYY-MM-DD HH:MM` | `2026-11-24 14:05` |
| Boolean | `TRUE`/`FALSE` | |
| Integer | Python `int` | `45` |
| Text | sanitized string (4.3) | |

### 4.3 Text sanitization (MUST)

Any free text from the model or user (Title, Notes, Location, Reason, review fields, Inbox text) goes through `sanitize_text(s, max_len)`:

1. Normalize Unicode (NFC). Replace newlines and tabs with spaces. Collapse repeated whitespace. Strip.
2. Truncate to `max_len`.
3. **Formula-injection guard:** with `USER_ENTERED`, text starting with `=`, `+`, `-` or `@` would be parsed as a formula or number. Prefix such text with a single apostrophe `'`. Google stores the apostrophe as a "text" marker, not as visible content.
4. Text that would otherwise be parsed as a number or date (e.g. a title `2026-01-01` or `1/2`) is also prefixed with `'`. Check with simple regexes for ISO dates, `\d+/\d+`, and pure numbers.

### 4.4 Digits and language

The owner writes in Farsi too. Before validating dates, times and integers in tool arguments, convert Persian (`۰–۹`) and Arabic-Indic (`٠–٩`) digits to ASCII. Tool arguments are always Gregorian ISO dates. Jalali dates are never stored.

---

## 5. Domain rules (`planner/rules.py`)

All rules are pure functions over already-loaded data, unit-testable without I/O.

### 5.1 Enums

Validate Area, Priority, statuses, Type, Flexibility, Level and habit names against the **live Settings lists** (section 3.9), case-sensitive after trimming. On failure return error `invalid_argument` with the allowed values in `message`.

### 5.2 Time sanity

- `0 ≤ start < end ≤ 1440` minutes.
- Duration 5–720 minutes.
- Dates must be within `[today − 366 days, today + 730 days]`.

### 5.3 Day bounds

`DayStart ≤ start` and `end ≤ DayEnd`. If violated, error `outside_hours`, unless `Type` is `Routine` with a title that relates to sleep or wind-down (the owner's own routines); the bot never schedules non-routine items in QuietHours.

### 5.4 Overlap

Two non-cancelled Schedule rows on the same date overlap if `startA < endB and startB < endA`. Rows with `Status in (Cancelled, Skipped)` are ignored.

- Creating or moving into an overlap → error `overlap`. Include the conflicting item (`id`, `title`, `start`, `end`, `flexibility`) and `suggestions` (up to 3 free slots of the same length on the same date, from 5.6).
- The bot MUST NOT silently move other items to make room. If the owner asks to bump a Flexible item, the model calls `planner_move` on it explicitly, then retries.

### 5.5 Fixed protection

Moving, shortening, cancelling or rescheduling an item with `Flexibility == Fixed` requires confirmation (section 12). The tool returns `needs_confirmation` instead of writing. This also applies to Tasks with `Flexibility == Fixed` when changing `PlannedDate` or `DueDate`, or cancelling.

### 5.6 Free-slot finder

`find_free_slots(day_events, date, minutes, area, settings, now, earliest=None, latest=None, limit=3) -> list[(start, end)]`

1. Window = `[max(DayStart, earliest), min(DayEnd, latest)]`. If `date == today`, the window starts no earlier than `now` rounded **up** to the next 15 minutes.
2. Busy intervals = non-cancelled events that day, each expanded by `BufferMinutes` on both sides, clipped to the window, merged.
3. Free gaps = window minus busy.
4. In each gap, candidate starts are the gap start rounded up to 15 minutes, then every 30 minutes after it, while `start + minutes ≤ gap end`.
5. Ordering preference:
   - `area == "Work"` on a weekday: candidates inside WorkHours first, earliest first.
   - Other areas on a weekday: candidates outside WorkHours first, earliest first.
   - Weekends: earliest first, but prefer starts ≥ 09:00.
6. Return the first `limit` non-overlapping candidates.

### 5.7 Focus cap

The sum of durations of non-cancelled rows with `Type in (Focus, Task block)` on a date, plus the new or moved block, must be ≤ `MaxFocusHoursPerDay × 60`. If exceeded, the write tool returns `needs_confirmation` with summary "This would make N h of focused work on <day> (limit M h)."

### 5.8 Routine days

`routine_runs_on(days_text, weekday) -> bool`. `Daily` = all, `Weekdays` = Mon–Fri, `Weekends` = Sat–Sun, otherwise a comma list of 3-letter English day names (case-insensitive, spaces ignored). Unknown tokens → treat the routine as invalid and skip it with a warning.

### 5.9 IDs

`next_id(prefix, existing_ids, width)`: highest numeric suffix among IDs with that prefix, plus 1, zero-padded to `width`. Widths: `T` 4, `E` 4, `I` 4, `RV` 4, `G` 3, `R` 2. IDs are never reused. Cancelled rows still count.

### 5.10 Capacity

Before appending, if `next_row > max_row` for that tab (Tasks 1000, Schedule 3000, Goals 301, Inbox 1000, Reviews 1000, ChangeLog 5000), return error `capacity` and tell the owner. If after the write a tab is ≥ 90% full, add `"warning"` to the result.

---

## 6. Behaviour rules the model must follow (prompt contract)

These go in the system prompt (section 15) and are also enforced in code where possible:

1. Use planner tools for anything about the owner's schedule, tasks, habits, goals or plans. Never guess the planner's contents; call `planner_get_agenda` first.
2. Resolve relative dates and times ("tomorrow", "after work", "this weekend") into exact ISO values using the current local date and time in the prompt.
3. Pick sensible defaults and say so in the reply (e.g. "I put it at 17:30, after work"), or ask **one** short question when a missing detail really matters.
4. Never claim a change happened unless a tool returned `ok: true` in this turn (the existing prompt already says this).
5. After changes, reply in 1–3 short lines: what changed (with day and time) and the ID only if useful. The bot adds the Undo button automatically.
6. On `needs_confirmation`, tell the owner what needs approval and that a button is attached. Don't call the tool again with `force`; there is no `force` argument.
7. Reply in the owner's language. Tool arguments are always English enum values and ISO dates.

---

## 7. Architecture inside the repo

### 7.1 Message flow (unchanged skeleton)

```
Telegram → /telegram/webhook → Queue (max_concurrency 1) → app.handle_update
  → _run_graph: load_context → agent ⇄ tools → finalize
       tools_node → executor.run_tool_call → TOOLS[name].handler(ctx, args)
                                               └─ planner tools → PlannerService
  → save_response (now also stores buttons) → _deliver (sends reply + buttons)
Callback buttons → app._callback → planner confirm / undo
Cron (hourly) → scheduled() → morning brief / evening check-in (deterministic, no LLM)
```

The queue's `max_concurrency: 1` and `max_batch_size: 1` already serialize all writes from the bot. Keep them.

### 7.2 New and changed files

```
src/planner/__init__.py
src/planner/google_auth.py     # service-account OAuth (JWT RS256 via Web Crypto), token cache
src/planner/sheets.py          # minimal Sheets REST client (batchGet / batchUpdate), SheetsError
src/planner/values.py          # serial <-> date/time, sanitize_text, digit normalization
src/planner/model.py           # dataclasses: Task, Event, Goal, Routine, InboxItem, Review, Settings
src/planner/schema.py          # TAB definitions: names, columns, ID prefix/width, max rows
src/planner/store.py           # read tabs -> objects; per-message cache
src/planner/rules.py           # section 5
src/planner/writes.py          # WriteBatch: ops by ID, commit resolves rows, ChangeLog, op key
src/planner/service.py         # PlannerService: high-level operations used by tools
src/planner/tools.py           # Tool(...) definitions registered into tools.registry.TOOLS
src/planner/briefing.py        # morning brief / evening check-in text (no LLM)
migrations/0004_planner.sql
tests/fakes.py                 # + FakeSheets, FakeSigner; FakeHttp routes Google URLs
tests/unit/test_planner_values.py
tests/unit/test_planner_rules.py
tests/unit/test_planner_store.py
tests/unit/test_planner_writes.py
tests/unit/test_planner_tools.py
tests/unit/test_planner_flow.py   # end-to-end via app.handle_update with scripted Gemini
docs/LIFE_PLANNER_AGENT_GUIDE.md  # copied from the owner
docs/PLANNER_MODULE_SPEC.md       # this file
```

Changed: `src/http_client.py`, `src/config.py`, `src/app.py`, `src/entry.py`, `src/graph/state.py`, `src/graph/nodes.py`, `src/graph/builder.py`, `src/tools/registry.py`, `src/tools/executor.py`, `src/prompts.py`, `src/storage/repo.py`, `wrangler.jsonc`, `README.md`.

### 7.3 Why plain tools, not a subgraph

Planner actions are short (one or two reads, one write). The repo already has validated tools, an audit log and confirmation buttons. A subgraph with `interrupt()` would duplicate the confirmation mechanism and add checkpoint size and CPU. Revisit only if `plan_week` grows into a multi-turn dialogue.

---

## 8. Configuration and secrets

### 8.1 New variables and secrets

| Name | Kind | Example | Notes |
|---|---|---|---|
| `PLANNER_SHEET_ID` | var | `1AbC…xyz` | Spreadsheet ID from the sheet URL. Empty → planner disabled. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **secret** | full JSON key | Must contain `client_email`, `private_key`, `token_uri`. |
| `PLANNER_BRIEF_HOUR` | var | `7` | Local hour for the morning brief; empty → off. |
| `PLANNER_CHECKIN_HOUR` | var | `21` | Local hour for the evening check-in; empty → off. |

Add them to both `staging` and `production` in `wrangler.jsonc` (vars) and document the secret in `README.md`. **Staging MUST point to a copy of the sheet** ("Planner-TEST"), never the real one.

### 8.2 `Config` changes (`src/config.py`)

Add fields `planner_sheet_id: str | None`, `google_service_account_json: str | None` (keep it out of `repr`, using `field(repr=False)`), `planner_brief_hour: int | None`, `planner_checkin_hour: int | None`, plus a property `planner_enabled` (sheet ID and JSON both present). Parse in `load_config`. Do **not** fail config loading when they are missing. The planner is simply disabled.

### 8.3 Owner setup steps (put in README)

1. In Google Cloud Console: create a project → enable **Google Sheets API** → create a **service account** → create a **JSON key**.
2. Open the planner sheet → **Share** → add the service account's `client_email` as **Editor**. Do the same for the staging copy.
3. In Cloudflare, set secret `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the whole JSON) and var `PLANNER_SHEET_ID` for each Worker.
4. In the sheet, type `RequestID` in `ChangeLog!J1`.
5. Deploy staging, then send "What's on today?" to the staging bot.

---

## 9. HTTP layer changes (`src/http_client.py`)

Add one general method to the `Http` protocol and both implementations:

```python
async def request(self, method: str, url: str, *, body: str | bytes | None = None,
                  headers: dict[str, str] | None = None) -> HttpResponse: ...
```

- `WorkerHttp.request` uses `workers.fetch(url, method=..., headers=..., body=...)` (lazy import) and returns `HttpResponse(status, text)`.
- `post_json` stays as is (existing callers). It MAY be reimplemented on top of `request`.
- `FakeHttp.request` (tests) routes:
  - `https://oauth2.googleapis.com/token` → `{"access_token": "test-token", "expires_in": 3599, "token_type": "Bearer"}`
  - `https://sheets.googleapis.com/v4/spreadsheets/...` → `FakeSheets` (section 18.1)
  - anything else → `AssertionError`.
- Record calls in `FakeHttp.calls` as today.

---

## 10. Google access

### 10.1 `planner/google_auth.py`

```python
class GoogleAuthError(RuntimeError): ...

class Signer(Protocol):
    async def sign_rs256(self, data: bytes) -> bytes: ...

class WebCryptoSigner:
    """RS256 with the Workers Web Crypto API (no Python crypto packages)."""
    def __init__(self, private_key_pem: str): ...
    async def sign_rs256(self, data: bytes) -> bytes: ...

class ServiceAccountAuth:
    SCOPE = "https://www.googleapis.com/auth/spreadsheets"
    def __init__(self, service_account_json: str, http: Http, signer: Signer | None = None, clock=time.time): ...
    async def token(self) -> str: ...   # cached; refreshed 60 s before expiry
```

JWT details:
- Header `{"alg": "RS256", "typ": "JWT"}`.
- Claims `{"iss": client_email, "scope": SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600}`.
- `signing_input = b64url(header_json) + "." + b64url(claims_json)` (compact JSON, base64url without padding).
- `jwt = signing_input + "." + b64url(signature)`.

Token request:
- `POST token_uri` (default `https://oauth2.googleapis.com/token`)
- header `content-type: application/x-www-form-urlencoded`
- body `grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=<jwt>`
- Response JSON gives `access_token` and `expires_in`. Non-200 → `GoogleAuthError` with status. 5xx and 429 are retryable.

Web Crypto signing (inside `WebCryptoSigner`, lazy imports):
1. PEM → DER: strip the `-----BEGIN PRIVATE KEY-----` / `-----END PRIVATE KEY-----` lines and whitespace, then `base64.b64decode`.
2. `from js import crypto, Object, Uint8Array`, `from pyodide.ffi import to_js`.
3. `key = await crypto.subtle.importKey("pkcs8", to_js(der), to_js({"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"}, dict_converter=Object.fromEntries), False, to_js(["sign"]))`. Cache the `CryptoKey` on the instance.
4. `sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, to_js(data))`, then `bytes(Uint8Array.new(sig).to_py())`. Check the exact conversion on staging; `to_js(bytes)` must produce a `Uint8Array`.

Token cache: keep it in a module-level dict keyed by `client_email` (Worker isolates are reused, as `_services` already relies on). Never log tokens or key material. Error messages include status codes only.

### 10.2 `planner/sheets.py`

```python
class SheetsError(RuntimeError):
    def __init__(self, message: str, status: int | None, retryable: bool): ...

class SheetsClient:
    BASE = "https://sheets.googleapis.com/v4/spreadsheets/{sid}"
    def __init__(self, sheet_id: str, auth: ServiceAccountAuth, http: Http): ...
    async def batch_get(self, ranges: list[str]) -> dict[str, list[list[Any]]]: ...
    async def batch_update(self, data: list[tuple[str, list[list[Any]]]]) -> None: ...
```

- `batch_get`: `GET {BASE}/values:batchGet?ranges=<r1>&ranges=<r2>&valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=SERIAL_NUMBER&majorDimension=ROWS`. URL-encode each range with `urllib.parse.quote(r, safe="")`. Return `{requested_range: values}`, using the **requested** range strings as keys (Google normalizes them in `valueRanges[i].range`, so map by index). A missing `values` key means an empty list.
- `batch_update`: `POST {BASE}/values:batchUpdate` with JSON `{"valueInputOption": "USER_ENTERED", "includeValuesInResponse": false, "data": [{"range": r, "majorDimension": "ROWS", "values": v} ...]}`.
- Header `authorization: Bearer <token>`. On 401: refresh the token once and retry. 429 and 5xx → `SheetsError(retryable=True)`. Other statuses → not retryable. Include Google's `error.message` (≤ 300 chars).
- Ranges always quote tab names: `'Tasks'!A2:O1000`.

### 10.3 Retry integration (`src/app.py`)

Extend `is_retryable` with `SheetsError` and `GoogleAuthError` (use their `retryable` flag). This lets the queue redeliver on a temporary Google outage. **Section 11.3 guarantees retries don't duplicate writes.**

---

## 11. Reading and writing data

### 11.1 Schema (`planner/schema.py`)

A single source of truth:

```python
@dataclass(frozen=True)
class TabDef:
    name: str
    columns: tuple[str, ...]      # header names in order, A..
    id_prefix: str | None         # "T", "E", ... None for Habits/ChangeLog
    id_width: int
    max_row: int
    def col(self, header: str) -> str: ...   # header -> column letter
    @property
    def last_col(self) -> str: ...
```

Define `TASKS`, `SCHEDULE`, `GOALS`, `ROUTINES`, `HABITS`, `INBOX`, `REVIEWS`, `CHANGELOG`, `GOALCALC` exactly as in section 3 (column lists verbatim).

### 11.2 Store and cache (`planner/store.py`)

`PlannerStore(sheets: SheetsClient)` with a **per-message cache**: one store instance per graph run (section 14).

- `await settings()` reads Settings once per message and returns a `Settings` dataclass with parsed values and lists. If parsing fails (e.g. WorkHours malformed), use defaults from section 3.9 and log a warning.
- `await load(tab)` reads `'Tab'!A2:<last_col><max_row>` once and returns objects with `.row` (sheet row number). Skip rows whose ID cell is empty.
- `await load_many([tabs])` uses one `batch_get` for several tabs. Prefer this to keep the HTTP calls per message to 1–2 reads + 1 write.
- `invalidate(tab)` is called after a commit touching that tab.
- `await habits_header()` reads `'Habits'!A1:K1`. `await habit_row(date)` computes the row as `2 + (date − 2026-11-01).days`, then **verifies** by reading `'Habits'!A{row}` (cheap). If the date doesn't match, fall back to loading column A and searching.
- `await goal_progress()` reads `'_GoalCalc'!A2:I301` and returns `{goal_id: progress | None}`.

**Read only what a tool needs.** `get_agenda` loads Schedule + Tasks (+ Habits rows for the range). It does not load Goals or Inbox.

### 11.3 Writes (`planner/writes.py`)

Writes are described **by ID, not by row**. Row numbers are resolved at commit time from a fresh read, so human edits made in between can't make the bot write to the wrong row.

```python
@dataclass
class Create:   tab: TabDef; values: dict[str, Any]        # ID assigned at commit
@dataclass
class Update:   tab: TabDef; item_id: str; changes: dict[str, Any]; action: str  # update|move|complete|cancel
@dataclass
class SetHabit: date: date; habit: str; value: bool

class WriteBatch:
    def __init__(self, op_key: str, reason: str, now_local: datetime): ...
    def create(...) -> str            # returns a placeholder like "@1"
    def update(...)
    def set_habit(...)
    async def commit(self, sheets: SheetsClient, store: PlannerStore) -> CommitResult
```

`commit()`:
1. For every tab touched, read its column A (`'Tab'!A2:A<max_row>`), plus the target rows for updates (to get old values for the ChangeLog), plus `ChangeLog!A2:A5000` and `ChangeLog!J1`, **in one `batch_get`**.
2. Check `ChangeLog!J1 == "RequestID"`, otherwise raise `PlannerError("sheet_not_ready")`.
3. Resolve each `Update.item_id` to its current row. A missing ID raises `PlannerError("not_found")`.
4. For each `Create`: compute the next ID (5.9) and the next row (last non-empty A + 1, then +1 for each further create in the same tab), and check capacity (5.10). Fill in `CreatedBy="ai"`, `CreatedAt`/`UpdatedAt`/`CapturedAt` as the tab requires.
5. For each `Update`: also set `UpdatedAt` (if the tab has it). Build one range per changed cell (e.g. `'Tasks'!F13`).
6. Append ChangeLog rows: one row per changed field for updates; one row (Field empty, NewValue = Title or Text) per create; one per habit cell. Columns: Timestamp=now, Actor=`ai`, Action, Tab, ItemID, Field, OldValue (display string), NewValue (display string), Reason, RequestID=`op_key`.
7. Send **one** `batch_update` with all ranges.
8. Invalidate the touched tabs in the store. Return `CommitResult(created_ids=..., placeholders={"@1": "T-0014"}, changed_ids=...)`.

Display strings for OldValue/NewValue: dates `YYYY-MM-DD`, times `HH:MM`, booleans `TRUE`/`FALSE`, everything else `str()`.

### 11.4 Retry safety (idempotency), MUST

Problem: a planner write can succeed and a later step can fail (e.g. Gemini 429 on the follow-up call). The queue then re-runs the whole message, and a naive tool would write again.

Solution, combining D1 and the sheet's ChangeLog:

- `op_key = f"u{update_id}:{tool_name}:{args_hash(args)}"` (reuse `tools.registry.args_hash`). If `update_id` is None (cron/tests), use a random UUID.
- New D1 table `planner_ops(op_key PRIMARY KEY, status, result_json, created_at, updated_at)` (migration in section 16).
- Write tools run through `PlannerService.run_op(op_key, fn)`:
  1. `SELECT` the op. If `status = 'done'`, return the stored `result_json` with `"replayed": true`. **Do not touch the sheet.**
  2. If `status = 'started'` (a previous attempt crashed mid-way), read `ChangeLog!J2:J5000`. If any row equals `op_key`, the write landed: rebuild a minimal result from those ChangeLog rows (`{"ok": true, "summary": "Already done.", "changed": [...ids]}`), mark the op `done` and return it. Otherwise, continue to step 3.
  3. Upsert `status = 'started'`, run the op (`fn` builds the WriteBatch and commits), store `result_json` and `status = 'done'`.
  4. If `fn` raises `PlannerError` (a validation-type error), store the error result as `done` too, so retries return the same error. If it raises a retryable error, leave the op `started` and re-raise.
- Read-only tools do not use `run_op`.
- `repo.cleanup()` deletes `planner_ops` older than 30 days.

---

## 12. Confirmations and Undo (Telegram buttons)

### 12.1 Storage changes

Migration (section 16):
- `pending_actions` gets a `payload TEXT` column (JSON).
- `telegram_updates` gets a `response_buttons TEXT` column (JSON), so a retried delivery re-sends the same buttons.

`Repo` changes:
- `create_pending(chat_id, action, ttl_minutes=10, payload: dict | None = None) -> str` stores `json.dumps(payload)`.
- `consume_pending(token, chat_id)` returns `{"action": ..., "payload": dict | None}` **or** keep the current return type and add `consume_pending_full`. Update existing callers and tests.
- `save_response(..., buttons: list | None = None)` stores `response_buttons`. `claim_update` returns it too.

### 12.2 How a tool asks for confirmation

When a rule requires confirmation (5.5, 5.7, `plan_week`), the tool:
1. Builds a **fully resolved** payload describing the exact op: `{"op": "move", "args": {...validated args...}, "confirmed": true, "summary": "..."}`.
2. Calls `repo.create_pending(chat_id, "planner_confirm", ttl_minutes=30, payload=payload)`.
3. Appends `("✅ Confirm", f"ok:{token}")` and `("✖️ Cancel", f"no:{token}")` to `ctx.buttons` (section 14).
4. Returns `{"ok": false, "needs_confirmation": true, "summary": "<what will happen>"}` without an `"error"` key, so `executor._msg` marks it a success-status ToolMessage.

`chat_id`: private chats have `chat_id == user_id`. Pass `chat_id` through `ToolContext` anyway (add the field).

### 12.3 Undo button

After any successful write op (not replayed), the tool registers `("↩️ Undo", f"ok:{token}")`, where the token is `create_pending(chat_id, "planner_undo", ttl_minutes=120, payload={"op_key": op_key})`. If one message produced several write ops, attach **one** Undo button whose payload lists all op keys: `{"op_keys": [...]}`. Deduplicate in `finalize` (section 14).

### 12.4 Button rows and delivery

`_deliver` / `_reply` gain `buttons: list[list[tuple[str, str]]] | None`. Attach them to the **last** chunk sent. Layout: confirmations on one row (`Confirm`, `Cancel`), Undo on its own row. Telegram `callback_data` must be ≤ 64 bytes; `ok:` + a 16-char token fits.

### 12.5 Callback handling (`app._callback`)

Extend the existing flow:

- `action == "planner_confirm"`: if verb is `no`, edit the message to "Cancelled. Nothing was changed." Otherwise run `PlannerService.execute_confirmed(payload)` with `op_key = f"cb:{token}"`, which bypasses the confirmation rule and **still checks every other rule**, e.g. overlap. Edit the message to the result summary and add an Undo button for that op (send a new short message with the button, since `edit_message` removes buttons).
- `action == "planner_undo"`: run `PlannerService.undo(op_keys)`, edit the message to the summary. If the verb is `no`, ignore it (Undo has no Cancel button).
- `consume_pending` is already atomic, so double taps are harmless.

### 12.6 Undo semantics (`PlannerService.undo(op_keys)`)

1. Read the ChangeLog rows with `RequestID in op_keys`.
2. If a ChangeLog row with `Action = "undo"` already references any of these op keys (its `Reason` contains `undo:<op_key>`), return "Already undone."
3. Process the rows in **reverse** order:
   - `update`, `move`, `complete`, `cancel`: set Field back to OldValue, but **only if the current value still equals NewValue**. Otherwise skip that field and report "changed since, not reverted: …".
   - `create`: soft-remove the item (Tasks/Schedule → `Status = Cancelled`; Goals → `Dropped`; Inbox → `Discarded`; Reviews → leave as is and report).
   - Habit cell: restore the old value (empty when OldValue was empty).
4. Commit as one WriteBatch with `Action = "undo"`, `Reason = "undo:<op_key>"` for each source op, and its own new op key.

### 12.7 The general `planner_undo_last` tool

For "undo that" in text: find the most recent op key in ChangeLog with `Actor = ai`, `Action != undo` and not already undone, and undo it (12.6). This needs no confirmation.

---

## 13. Tools (`planner/tools.py`)

### 13.1 Registration and validation changes

- Planner tools are `tools.registry.Tool` instances. Merge them into `TOOLS` **only when the planner is enabled**: change `declarations()` to take `enabled_extra: bool` (or build the dict once in `Services` and pass it in `Deps`). Disabled → the model never sees planner tools, and the prompt says the planner isn't connected.
- Extend `registry.validate` with:
  - `"type": "boolean"`.
  - `"type": "number"`.
  - Local-only keys (stripped from declarations like `maxLength`): `"format": "date" | "time"`, `"minimum"`, `"maximum"`, `"pattern"` (regex for IDs).
  - Digit normalization (4.4) for strings with `format` and for integers passed as strings.
  - `date`: strict `YYYY-MM-DD`, a valid calendar date. `time`: `HH:MM`, 00:00–24:00.
- Raise `executor`'s per-message budget: `Deps.max_tool_calls` from 4 to **8**, and `graph.builder.RECURSION_LIMIT` from 16 to **24**.

### 13.2 Result envelope (all planner tools)

```json
{"ok": true,  "summary": "one-line human summary", "changed": ["T-0014"], "data": {...}, "warning": "optional"}
{"ok": false, "error": "overlap", "message": "human text", "conflict": {...}, "suggestions": [{"start":"14:10","end":"15:10"}]}
{"ok": false, "needs_confirmation": true, "summary": "Move FIXED '1:1 with manager' to Thu Nov 26 10:00–11:00?"}
```

Error codes: `invalid_argument`, `not_found`, `overlap`, `outside_hours`, `out_of_range`, `capacity`, `sheet_not_ready`, `sheet_unavailable`, `planner_disabled`.

Validation errors raise `ToolInputError` (existing path). Domain errors return `{"ok": false, "error": ...}`. Unexpected exceptions fall into the executor's generic "the tool failed; nothing was saved" path. That message is only true if no commit happened, which holds because commit is the last step and a single API call.

Keep results compact. Times as `HH:MM`, dates as `YYYY-MM-DD` plus a weekday name where helpful. Never return full tabs.

### 13.3 Tool list (v1)

Descriptions below are the exact `description` strings to use, written for the model. Parameter tables: name, type, required, notes.

---

**`planner_get_agenda`** (read)
Description: "Get the owner's planner for one or more days: time blocks/events, to-dos planned or due on those days, habit status, and overdue tasks. Call this before answering any question about the owner's schedule or before changing it."

| param | type | req | notes |
|---|---|---|---|
| start_date | string, format date | yes | |
| end_date | string, format date | no | default = start_date; max range 14 days |

Returns `data.days[]`, each with:
- `date`, `weekday`
- `events[]`: `{id, start, end, title, type, area, flexibility, status, linked_id, location}`, sorted by start, excluding Cancelled
- `tasks[]`: `{id, title, area, priority, status, due, planned, est_minutes}`. A task is listed on its PlannedDate; or on its DueDate if PlannedDate is empty. Excludes Cancelled. Sorted with Done last, then by priority.
- `habits{name: true|false|null}`

Plus `data.overdue[]`: tasks with `due < today`, Status not Done or Cancelled, at most 10. `summary` like "Tue Nov 24: 9 blocks, 2 tasks (0 done), 1 overdue".

---

**`planner_find_free_slots`** (read)
Description: "Find free time on a day for a block of a given length, respecting the owner's work hours, day start/end and buffer. Use before scheduling when no time was given."

| param | type | req | notes |
|---|---|---|---|
| date | date | yes | |
| minutes | integer 5–720 | yes | |
| area | string (Areas) | no | changes ordering (5.6) |
| earliest | time | no | |
| latest | time | no | |

Returns `data.slots[]` (`{start, end}`, up to 5).

---

**`planner_add_task`** (write)
Description: "Create a to-do in the planner. Use when the owner wants to remember or do something without a fixed time. Set planned_date only if the owner named a day."

| param | type | req | notes |
|---|---|---|---|
| title | string ≤120 | yes | short, starts with a verb |
| area | string (Areas) | yes | |
| priority | enum P1–P4 | no | default P2 |
| due_date | date | no | |
| planned_date | date | no | |
| est_minutes | integer 5–720 | no | |
| goal_id | string pattern `^G-\d{3}$` | no | must exist |
| notes | string ≤500 | no | |

Behaviour: dedupe guard. If a non-Done, non-Cancelled task with the same title (case-insensitive) and same planned_date already exists, return it with `ok: true, summary: "Already in your list: …"`, `changed: []`. Otherwise create it with Status `Not started` and Flexibility `Flexible`.

---

**`planner_update_task`** (write)
Description: "Change details of an existing to-do (title, area, priority, dates, estimate, goal, notes, or status In progress/Waiting/Not started). To finish or cancel a task use planner_complete_task or planner_cancel."

Params: `task_id` (pattern `^T-\d{4}$`, required) plus any of the add_task fields, and `status` ∈ {Not started, In progress, Waiting}. To clear a date, pass `"none"` (allow it in the date validator for this tool only). Fixed tasks: changing due/planned dates → confirmation.

---

**`planner_complete_task`** (write)
Description: "Mark a to-do as done. Also marks its planned time blocks for today or earlier as done."

Params: `task_id` (required).
Effect: Status `Done`, CompletedDate = today. Schedule rows with `LinkedID == task_id`, `Status == Planned` and `Date ≤ today` → `Done`. Future Planned blocks for that task → `Cancelled` (log it in the summary).

---

**`planner_schedule`** (write)
Description: "Put something on the calendar at a specific date and time: a time block for a to-do (pass task_id), a new event or appointment, or a focus block. If start is omitted, the first suitable free slot is used."

| param | type | req | notes |
|---|---|---|---|
| date | date | yes | |
| start | time | no | if omitted → first slot from 5.6 |
| end | time | no | |
| minutes | integer 5–720 | no | used when end is omitted; default = task est_minutes or 60 |
| task_id | pattern `^T-\d{4}$` | no | links block; title/area default from the task |
| title | string ≤120 | cond. | required if no task_id |
| area | Areas | cond. | required if no task_id |
| type | EventType | no | default `Task block` with task_id, otherwise `Event` |
| flexibility | Fixed/Flexible | no | default: `Fixed` for type Event, otherwise `Flexible` |
| location | string ≤120 | no | |
| notes | string ≤500 | no | |

Rules: 5.2, 5.3, 5.4 (with suggestions), 5.7.
Effect: create a Schedule row with Status Planned. If `task_id` is set, also set the task's PlannedDate to `date` (and Status `Not started` → unchanged). Result summary e.g. "Scheduled 'Q4 report' Wed Nov 25 09:00–11:00".

---

**`planner_move`** (write)
Description: "Move or resize an existing calendar item (event, time block, routine instance) to another date and/or time. Keeps its length if only a new start is given."

| param | type | req |
|---|---|---|
| event_id | pattern `^E-\d{4}$` | yes |
| date | date | no |
| start | time | no |
| end | time | no |
| minutes | integer | no |

At least one of date, start, end or minutes is required. Rules: 5.5 (Fixed → confirmation), 5.4 (excluding the item itself), 5.3, 5.7. If the item is a task block, also update the task's PlannedDate when the date changes. Action logged as `move`.

---

**`planner_cancel`** (write)
Description: "Cancel a to-do, calendar item, goal or inbox item by its ID. Nothing is deleted; the item is marked cancelled."

Params: `item_id` (pattern `^(T-\d{4}|E-\d{4}|G-\d{3}|I-\d{4})$`), `reason` (string ≤200, optional).
Effect by prefix:
- `T` → Status `Cancelled`, plus that task's future Planned blocks → `Cancelled`
- `E` → Status `Cancelled`
- `G` → Status `Dropped`
- `I` → Status `Discarded`

Fixed T/E → confirmation.

---

**`planner_log_habit`** (write)
Description: "Record whether the owner did a habit on a day (default today). Habit names come from the planner's Settings."

Params: `habit` (string, required; matched case-insensitively against the habit header names, error lists the valid names), `date` (date, optional; default today), `done` (boolean, optional; default true).
For several habits at once the model calls this tool several times.

---

**`planner_capture`** (write)
Description: "Save a quick idea or note to the planner's Inbox for later sorting. Use when the owner says something vague like 'sometime I should…' with no clear task, day or goal."

Params: `text` (string ≤300, required). Creates an Inbox row with Status `New`.

---

**`planner_goals`** (read)
Description: "List the owner's goals with progress. Filter by level and/or year."

Params: `level` (GoalLevel, optional), `year` (integer, optional; default PlanningYear for Year/Quarter, current year otherwise), `include_inactive` (boolean, default false).
Returns `{id, title, level, area, parent, start, end, metric, target, current, progress_pct, status}` (progress from `_GoalCalc`, rounded).

---

**`planner_update_goal`** (write)
Description: "Update a goal's progress number (current) or status (Active, Done, Paused, Dropped)."

Params: `goal_id` (required), `current` (number, optional), `status` (GoalStatus, optional). At least one is required.

---

**`planner_undo_last`** (write)
Description: "Undo the most recent change the assistant made in the planner. Use when the owner says 'undo', 'revert that' or similar."

No params. See 12.7.

### 13.4 Tools for v2 (phase 5)

- **`planner_list_inbox`** (read): Inbox rows with Status `New`.
- **`planner_mark_inbox`** (write): `inbox_id`, `status` (Processed/Discarded), `converted_to_id` (optional). The model processes the inbox by calling `planner_list_inbox`, then `planner_add_task`/`planner_schedule`/`planner_capture`, then `planner_mark_inbox`.
- **`planner_plan_week`** (write, always confirmation): param `week_start` (date, a Monday).
  - Builds a proposal:
    1. Routine instances for each Active routine on each matching day not already present (same `LinkedID` + date). Fixed → WindowStart; Flexible → the first free slot within the window.
    2. Rollover: list tasks with `PlannedDate < week_start` and Status open, proposing new PlannedDates spread Mon–Fri by priority, at most 3 P1/P2 per day.
    3. Report any day over the focus cap.
  - Stores the proposal as the pending payload (`ttl 120 min`) and returns `needs_confirmation` with a compact summary ("Add 38 routine blocks, move 4 unfinished tasks; Wed would have 4.5 h focus").
  - On confirm, applies everything as **one** WriteBatch with one op key.
- **`planner_add_review`** (write): Type, PeriodStart and the text fields of section 3.7.

---

## 14. Graph and context wiring

### 14.1 `Deps` (`graph/state.py`)

Add:
- `planner: Any = None` (a `PlannerService` for this run, or None when disabled)
- `chat_id: int = 0`
- `buttons: list[list[tuple[str, str]]] = field(default_factory=list)`
- `undo_ops: list[str] = field(default_factory=list)`

`max_tool_calls` default → 8.

### 14.2 `ToolContext` (`tools/registry.py`)

Add `planner: Any = None`, `chat_id: int = 0`, `buttons: list = None` (reference to `deps.buttons`) and `undo_ops: list = None` (reference to `deps.undo_ops`). Update the construction in `nodes.tools_node`.

### 14.3 `PlannerService` lifetime

- `Services.__init__` (in `app.py`) creates `SheetsClient` + `ServiceAccountAuth` once if the planner is enabled. They are long-lived, so the token is cached.
- `_run_graph` creates a fresh `PlannerService(sheets, store=PlannerStore(sheets), repo, tz, now)` per message and puts it in `Deps.planner`. The store's cache lives only for that message.
- Timezone: the owner's saved preference `timezone` if set, else `Settings!B3`, else `cfg.timezone`. "Today" and "now" are computed in that timezone once per message.

### 14.4 Finalize and buttons

In `nodes.finalize`: if `deps.undo_ops` is non-empty, create one `planner_undo` pending action (payload `{"op_keys": undo_ops}`) and append the Undo button row. Then `_run_graph` returns `(answer, conv, deps.buttons)`, and `_process` passes the buttons to `save_response` and `_deliver`.

Tool handlers must append a confirmation row to `ctx.buttons` at most once per pending token.

---

## 15. Prompt changes (`src/prompts.py`)

1. In "Tools and honesty", replace "You cannot set reminders, track expenses, read calendars, or browse the web yet" with a version that depends on whether the planner is enabled:
   - enabled: "You can read and change the owner's Life Planner (schedule, to-dos, habits, goals) only through the planner tools. You cannot send reminders, track expenses or browse the web yet."
   - disabled: keep the original sentence.
2. Append this section when enabled (`render_system_prompt` gets a `planner_enabled` flag):

```
Life Planner
- The owner's planner is a Google Sheet you access only through planner_* tools.
- For any question about their day, week, tasks, habits or goals, call planner_get_agenda or planner_goals first. Never guess.
- Turn words like "tomorrow", "tonight", "after work", "this weekend", "next week" into exact dates/times using the current local date/time above. Work hours are 09:00-17:00 unless a tool says otherwise. Weeks start on Monday.
- Tool arguments: dates YYYY-MM-DD (Gregorian), times HH:MM 24-hour, enum values in English exactly as listed, even when chatting in Farsi.
- If a detail is missing, choose a sensible default and say which (e.g. "I set it for 60 minutes"); ask at most one short question only if the choice really matters.
- If a tool reports an overlap, offer the suggested times instead of forcing it.
- If a tool says needs_confirmation, tell the owner what needs approval; buttons are attached automatically. Do not retry the tool.
- After a change, reply in 1-3 short lines: what changed, with weekday, date and time. Don't list IDs unless the owner asks. An Undo button is added automatically.
- Items marked Fixed are commitments; never suggest moving them casually.
```

Keep the prompt short. Do **not** inject the agenda into the prompt; tools fetch it.

---

## 16. Database migration (`migrations/0004_planner.sql`)

```sql
-- Planner: retry-safe sheet operations, confirmation payloads, buttons on stored replies.
CREATE TABLE IF NOT EXISTS planner_ops (
  op_key      TEXT PRIMARY KEY,
  status      TEXT NOT NULL,              -- started | done
  result_json TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
ALTER TABLE pending_actions ADD COLUMN payload TEXT;
ALTER TABLE telegram_updates ADD COLUMN response_buttons TEXT;
```

`Repo` additions: `get_op(op_key)`, `start_op(op_key)`, `finish_op(op_key, result)`. Add to `cleanup()`: `DELETE FROM planner_ops WHERE created_at < datetime('now', '-30 days')`.

---

## 17. Scheduled messages (cron)

### 17.1 Trigger

Add a second cron `"5 * * * *"` (hourly) to both environments in `wrangler.jsonc`, keeping `"17 9 * * *"` for cleanup. In `entry.scheduled(controller, ...)`, branch on `controller.cron`:
- `"17 9 * * *"` → existing cleanup.
- `"5 * * * *"` → `app.planner_tick(svc)`.

### 17.2 `app.planner_tick`

1. Return immediately if the planner is disabled or `authorized_user_ids` is empty.
2. Owner = the smallest ID in `authorized_user_ids`; chat_id = owner ID.
3. Compute the local hour (owner timezone, 14.3). Cron runs in UTC, so compute this in code; DST is handled automatically.
4. If `hour == PLANNER_BRIEF_HOUR` → send the morning brief. If `hour == PLANNER_CHECKIN_HOUR` → send the evening check-in.
5. Dedupe: D1 `planner_ops` key `f"brief:{date}"` / `f"checkin:{date}"`, so a re-run in the same hour doesn't send twice.

### 17.3 Message contents (`planner/briefing.py`, deterministic, **no Gemini call**)

Telegram HTML, short:

**Morning brief**
```
☀️ <b>Tuesday, Nov 24</b>
<b>Today</b>
06:00 Morning routine
09:00–11:00 Deep work: Q4 report draft
14:00 Prepare slides for team meeting
…(non-routine, non-break first; max 8 lines, then "+N more")
<b>To-do</b> (3): Finish Q4 report draft (P1) · Email team… · …
⚠️ Overdue: Book dentist appointment (due Nov 20)
<b>Habits</b>: Exercise · Read 20 min · Drink 2L water …
```

**Evening check-in**
```
🌙 <b>Shutdown</b> — 2 of 4 tasks done today.
Still open: Email team about holiday schedule · Pick next book
Reply e.g. "move the rest to tomorrow" or "done with the email".
Habits not logged yet: Read 20 min, Journal
```

These are plain sends via `svc.telegram.send_message`. The owner's text reply then goes through the normal agent flow.

---

## 18. Testing

### 18.1 `FakeSheets` (in `tests/fakes.py`)

An in-memory spreadsheet:
- `tabs: dict[str, list[list[Any]]]` (0-based grid, row 0 = header).
- `from_xlsx(path)` MAY load the real `Life_Planner.xlsx` with openpyxl if available in the test environment. Otherwise build fixtures in Python with `seed_minimal()`, which writes headers for all tabs, Settings values and lists, Habits dates 2026-11-01…2027-12-31, a `_GoalCalc` with column A = Goals IDs and I = None, and `ChangeLog!J1 = "RequestID"`.
- `handle(method, url, body)` implements:
  - `values:batchGet`: parse A1 ranges (`'Tab'!A2:O1000`, single cells, full rows). Return `UNFORMATTED_VALUE` semantics: dates and times stored as serial floats, trailing empties trimmed.
  - `values:batchUpdate`: `USER_ENTERED` semantics. `YYYY-MM-DD` → serial int; `HH:MM` → fraction; `YYYY-MM-DD HH:MM` → serial float; `TRUE`/`FALSE` → bool; numeric strings → numbers; a leading `'` → literal text without the apostrophe.
- Counters `get_calls`, `update_calls` for asserting "one write per message".
- `fail_next(status)` to simulate 429/500.

`FakeSigner.sign_rs256` returns `b"sig"`.

### 18.2 Unit tests (minimum set)

- **values:** serial ↔ date/time round-trips including `24:00`; Persian digits; sanitize_text (formula guard, date-like text, truncation).
- **rules:** overlap edge cases (touching blocks don't overlap); free slots with buffer, work hours, today-after-now, weekends; focus cap; routine day parsing; next_id gaps and widths.
- **store:** padding of short rows; skipping empty IDs; Settings parsing with malformed WorkHours (falls back to defaults); habit row verification fallback.
- **writes:**
  - creates get sequential IDs in one batch;
  - updates resolve rows by ID after a simulated human row change (the row moved);
  - ChangeLog rows have correct OldValue/NewValue and the RequestID;
  - exactly one `batch_update` per commit;
  - `sheet_not_ready` when J1 is missing;
  - capacity error.
- **idempotency:**
  - run an op, then run it again with the same op key → no second write, `replayed: true`;
  - simulate a crash after commit but before `finish_op` (raise after `batch_update`) → the next attempt finds RequestID in ChangeLog and does not write again;
  - a validation error result is replayed.
- **tools (scripted Gemini as in `test_tools.py`):**
  - "what's on today" → `planner_get_agenda` called with today's date;
  - add_task dedupe;
  - schedule with overlap returns suggestions;
  - moving a Fixed event creates a pending action, attaches buttons and doesn't write;
  - confirm callback executes and writes once; a second tap reports expired/used;
  - complete_task updates linked blocks;
  - log_habit unknown name lists valid names;
  - undo_last reverts an update, soft-cancels a create, skips fields changed since, and a second undo says "Already undone".
- **flow:**
  - `handle_update` end-to-end: the reply carries the Undo button; a retried delivery (`claim.response` path) re-sends the same buttons without re-running tools;
  - Gemini 429 after a successful write → `RetryLater` → second attempt produces no duplicate row.
- **cron:** `planner_tick` sends the brief only at the configured local hour, once per day.
- **registry:** new validator features (date, time, boolean, number, pattern, min/max) and that local-only keys are stripped from declarations.
- **auth:** token caching and refresh-before-expiry, using a fake clock; a 401 from Sheets triggers one refresh.

### 18.3 Staging evaluation script (manual, against "Planner-TEST")

Run each line as a Telegram message on staging, check the reply and the sheet, then press Undo where relevant. All MUST pass before production.

| # | Message | Expected |
|---|---|---|
| 1 | What's on today? | Lists today's blocks and to-dos; no writes |
| 2 | What does my week look like? | 7-day summary; one agenda call with a 7-day range |
| 3 | Add call the bank this week, it's important | Task P1 or P2, area Money, planned this week; Undo button |
| 4 | Undo | Task Cancelled; ChangeLog has an undo row |
| 5 | Put the Q4 report tomorrow morning for 2 hours | Task block linked to T-0001, inside work hours, no overlap |
| 6 | Schedule a dentist appointment Friday 10am | Event, Fixed, Health |
| 7 | Move the dentist to 3pm | Confirmation buttons (Fixed); Confirm → moved |
| 8 | Gym tomorrow at 9 | If overlapping deep work → offers alternatives |
| 9 | I finished the slides | complete_task on T-0007; linked block Done |
| 10 | I worked out and meditated today | Two habit cells TRUE |
| 11 | Cancel tonight's reading | Reading block Cancelled (Flexible, no confirm) |
| 12 | Sometime I should learn to sail | Inbox item |
| 13 | How are my goals going? | Year goals with % from _GoalCalc |
| 14 | I've saved $2,000 now | update_goal G-002 current=2000 |
| 15 | فردا ساعت ۸ صبح ورزش | Workout block tomorrow 08:00; reply in Farsi |
| 16 | Plan next week (phase 5) | Proposal + Confirm; on confirm routines added once |
| 17 | (send #5 twice quickly) | Second is handled after the first; no duplicate block |
| 18 | (with ChangeLog!J1 cleared) Add a task | Refuses with the setup message |

---

## 19. Implementation phases

**Phase 1: Plumbing and read-only agenda**
- `Http.request`, `google_auth`, `sheets`, `values`, `schema`, `model`, `store`, config, FakeSheets.
- `planner_get_agenda`, `planner_find_free_slots`, `planner_goals`; prompt flag; registry validator extensions; tool budget 8.
- ✅ Acceptance: unit tests pass; on staging, "What's on today?" returns correct data in under ~5 s; `/status` shows tool calls ok.

**Phase 2: Safe writes**
- Migration 0004, `rules`, `writes`, `run_op` idempotency, ChangeLog RequestID check.
- `planner_add_task`, `planner_update_task`, `planner_complete_task`, `planner_schedule`, `planner_move` (Flexible only for now), `planner_cancel` (Flexible only for now), `planner_log_habit`, `planner_capture`, `planner_update_goal`.
- ✅ Acceptance: eval rows 1–6 and 8–15 (without buttons) pass; the idempotency tests pass; one `batch_update` per message.

**Phase 3: Confirmations and Undo**
- Pending payloads, button delivery and storage, callback branches, Fixed-protection and focus-cap confirmations, undo, `planner_undo_last`.
- ✅ Acceptance: eval rows 3–4, 7, 17 and 18 pass; double tap safe; retry re-sends buttons.

**Phase 4: Daily rhythm**
- Hourly cron, briefing texts, dedupe.
- ✅ Acceptance: brief arrives once at the configured local hour on staging for 2 consecutive days.

**Phase 5: Inbox and weekly planning**
- `planner_list_inbox`, `planner_mark_inbox`, `planner_plan_week`, `planner_add_review`.
- ✅ Acceptance: eval row 16; running plan_week twice for the same week adds no duplicate routine rows.

After each phase, update `README.md` (features, secrets, commands) and the `/help` text, e.g. add "Ask me about your day, add tasks, move things — I'll update your planner."

---

## 20. Edge cases and decisions

| Case | Decision |
|---|---|
| Owner edits the sheet while the bot works | Row resolution happens at commit from a fresh read (11.3); undo skips fields changed since (12.6). |
| Owner sorted or filtered a data tab | Still correct, because writes resolve by ID. Appends go after the last non-empty A cell. |
| Duplicate IDs found (human error) | Reads keep the first occurrence and log a warning. Writes targeting a duplicated ID return `invalid_argument` asking the owner to fix the sheet. |
| Empty Settings lists | Fall back to the defaults in 3.9. |
| Model sends a weekday name instead of a date | Validator rejects it (`format: date`); the model retries with a date. |
| Model asks for an agenda range of 30 days | Reject with "max 14 days". |
| Block ending at midnight | `end = 24:00` (1440). Write "24:00". Reads of serial 1.0 → 1440. |
| A request spanning midnight (23:30–00:30) | `invalid_argument` ("split into two blocks"); the model may create two rows. |
| Timezone change by the owner | Use the saved preference (existing `save_user_preference`). |
| Google API down | Retryable error → queue retry with existing delays. The user gets the existing "something went wrong" message after the final attempt. |
| Service account lost access (403) | Not retryable. Reply: "I can't open the planner sheet. Check that it's shared with the service account." |
| Very large Schedule (≥ 2,500 rows) | Add `warning` to results (capacity, 5.10). Future optimisation: read only the `B` column first, then the needed rows. |
| Free-tier Gemini quota exhausted mid-op | Covered by idempotency (11.4). |
| Privacy | Never log sheet contents, tokens or key material. Tool audit logs keep using `args_hash` only. |

---

## 21. Definition of done

- All phases implemented; `pytest -q` green; CI green.
- The staging evaluation (18.3) passes completely on "Planner-TEST".
- No view tab, `Settings` or `_GoalCalc` cell is ever written (assert this in FakeSheets: writes to these tabs raise).
- Every bot change in the sheet has matching ChangeLog rows with a RequestID.
- A message produces at most one Sheets write call.
- README documents setup, secrets, the ChangeLog `RequestID` header step, and how to disable the planner (unset `PLANNER_SHEET_ID`).
