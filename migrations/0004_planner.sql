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
