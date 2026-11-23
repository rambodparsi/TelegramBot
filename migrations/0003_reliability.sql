-- Outbox: the generated reply is stored before sending, so a retry re-sends it
-- instead of re-running the model (which would duplicate the turn in graph state).
ALTER TABLE telegram_updates ADD COLUMN response TEXT;
ALTER TABLE telegram_updates ADD COLUMN response_prefix TEXT;
ALTER TABLE telegram_updates ADD COLUMN conversation_id TEXT;
ALTER TABLE telegram_updates ADD COLUMN kind TEXT;
ALTER TABLE telegram_updates ADD COLUMN user_text TEXT;
