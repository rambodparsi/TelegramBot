-- Application tables (LangGraph checkpoint tables are created by the saver itself).

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,                        -- short random id
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,                   -- Telegram user id
  thread_id TEXT NOT NULL UNIQUE,             -- telegram:{chat_id}:{id}
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conversations_active ON conversations(chat_id, is_active);

CREATE TABLE IF NOT EXISTS telegram_updates (
  update_id INTEGER PRIMARY KEY,
  chat_id INTEGER,
  status TEXT NOT NULL,                       -- received|done|ignored|failed
  attempts INTEGER NOT NULL DEFAULT 1,
  last_error TEXT,
  received_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_updates_status ON telegram_updates(status, received_at);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  update_id INTEGER,
  role TEXT NOT NULL,                         -- user|assistant
  kind TEXT NOT NULL,                         -- text|voice|photo|command
  content TEXT NOT NULL,
  telegram_message_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY,
  update_id INTEGER,
  role TEXT NOT NULL,                         -- model role: chat|vision|transcribe|light|deep
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  fallbacks INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
