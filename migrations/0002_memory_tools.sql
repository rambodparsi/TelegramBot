-- Long-term memory, tool audit, and one-time confirmations.

CREATE TABLE IF NOT EXISTS user_preferences (
  user_id INTEGER NOT NULL,                   -- Telegram user id
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT NOT NULL,                       -- tool|command
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS memory_facts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL,                       -- tool|command
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_facts_user ON memory_facts(user_id, deleted_at);

CREATE TABLE IF NOT EXISTS tool_executions (
  id INTEGER PRIMARY KEY,
  update_id INTEGER,
  tool_name TEXT NOT NULL,
  args_hash TEXT NOT NULL,
  status TEXT NOT NULL,                       -- ok|error|rejected
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_actions (
  token TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  action TEXT NOT NULL,                       -- e.g. delete_history
  expires_at TEXT NOT NULL,
  used_at TEXT
);
