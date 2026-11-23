"""Application-table queries (everything except LangGraph checkpoints)."""

from __future__ import annotations

import secrets
from typing import Any

from storage import d1


class Repo:
    def __init__(self, db: Any):
        self.db = db

    # --- updates (dedup) ----------------------------------------------------
    async def claim_update(self, update_id: int, chat_id: int | None) -> bool:
        """True if this update is new; False if it was already seen."""
        row = await d1.first(
            self.db,
            "INSERT INTO telegram_updates (update_id, chat_id, status) VALUES (?, ?, 'received') "
            "ON CONFLICT(update_id) DO NOTHING RETURNING update_id",
            [update_id, chat_id],
        )
        return row is not None

    async def finish_update(self, update_id: int, status: str, error: str | None = None) -> None:
        await d1.execute(
            self.db,
            "UPDATE telegram_updates SET status = ?, last_error = ?, updated_at = datetime('now') WHERE update_id = ?",
            [status, error[:500] if error else None, update_id],
        )

    # --- conversations --------------------------------------------------------
    async def active_conversation(self, chat_id: int, user_id: int) -> dict[str, Any]:
        row = await d1.first(
            self.db,
            "SELECT id, thread_id FROM conversations WHERE chat_id = ? AND is_active = 1 ORDER BY created_at DESC LIMIT 1",
            [chat_id],
        )
        return row or await self.new_conversation(chat_id, user_id)

    async def new_conversation(self, chat_id: int, user_id: int) -> dict[str, Any]:
        conv_id = secrets.token_hex(6)
        thread_id = f"telegram:{chat_id}:{conv_id}"
        await d1.execute(self.db, "UPDATE conversations SET is_active = 0 WHERE chat_id = ? AND is_active = 1", [chat_id])
        await d1.execute(
            self.db,
            "INSERT INTO conversations (id, chat_id, user_id, thread_id) VALUES (?, ?, ?, ?)",
            [conv_id, chat_id, user_id, thread_id],
        )
        return {"id": conv_id, "thread_id": thread_id}

    # --- messages & usage -------------------------------------------------------
    async def add_message(self, conversation_id: str, role: str, kind: str, content: str,
                          update_id: int | None = None, telegram_message_id: int | None = None) -> None:
        await d1.execute(
            self.db,
            "INSERT INTO messages (conversation_id, update_id, role, kind, content, telegram_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [conversation_id, update_id, role, kind, content, telegram_message_id],
        )

    async def add_usage(self, update_id: int | None, usage: dict[str, Any], error: str | None = None) -> None:
        await d1.execute(
            self.db,
            "INSERT INTO usage_events (update_id, role, model, input_tokens, output_tokens, fallbacks, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [update_id, usage.get("role", "chat"), usage.get("model"), usage.get("input_tokens"),
             usage.get("output_tokens"), usage.get("fallbacks", 0), error],
        )

    # --- long-term memory -------------------------------------------------------
    async def profile(self, user_id: int) -> dict[str, Any]:
        prefs = await d1.query(self.db, "SELECT key, value FROM user_preferences WHERE user_id = ? ORDER BY key", [user_id])
        facts = await d1.query(
            self.db,
            "SELECT id, content FROM memory_facts WHERE user_id = ? AND deleted_at IS NULL ORDER BY id",
            [user_id],
        )
        return {"preferences": {r["key"]: r["value"] for r in prefs}, "facts": facts}

    async def set_preference(self, user_id: int, key: str, value: str, source: str = "tool") -> None:
        await d1.execute(
            self.db,
            "INSERT INTO user_preferences (user_id, key, value, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, source = excluded.source, "
            "updated_at = datetime('now')",
            [user_id, key, value, source],
        )

    async def delete_preference(self, user_id: int, key: str) -> bool:
        row = await d1.first(self.db, "DELETE FROM user_preferences WHERE user_id = ? AND key = ? RETURNING key",
                             [user_id, key])
        return row is not None

    async def add_fact(self, user_id: int, content: str, source: str = "tool") -> int:
        existing = await d1.first(
            self.db, "SELECT id FROM memory_facts WHERE user_id = ? AND deleted_at IS NULL AND lower(content) = lower(?)",
            [user_id, content])
        if existing:
            return int(existing["id"])
        row = await d1.first(
            self.db, "INSERT INTO memory_facts (user_id, content, source) VALUES (?, ?, ?) RETURNING id",
            [user_id, content, source])
        return int(row["id"])

    async def count_facts(self, user_id: int) -> int:
        row = await d1.first(self.db, "SELECT count(*) AS n FROM memory_facts WHERE user_id = ? AND deleted_at IS NULL",
                             [user_id])
        return int(row["n"]) if row else 0

    async def delete_fact(self, user_id: int, fact_id: int) -> bool:
        row = await d1.first(
            self.db,
            "DELETE FROM memory_facts WHERE user_id = ? AND id = ? RETURNING id",
            [user_id, fact_id])
        return row is not None

    async def forget_everything(self, user_id: int) -> None:
        # Hard delete: "forget" must really remove the data.
        await d1.execute(self.db, "DELETE FROM memory_facts WHERE user_id = ?", [user_id])
        await d1.execute(self.db, "DELETE FROM user_preferences WHERE user_id = ?", [user_id])

    # --- tool audit ---------------------------------------------------------------
    async def log_tool(self, update_id: int | None, name: str, args_hash: str, status: str,
                       error: str | None = None) -> None:
        await d1.execute(
            self.db,
            "INSERT INTO tool_executions (update_id, tool_name, args_hash, status, error) VALUES (?, ?, ?, ?, ?)",
            [update_id, name, args_hash, status, error[:300] if error else None])

    # --- one-time confirmations -----------------------------------------------------
    async def create_pending(self, chat_id: int, action: str, ttl_minutes: int = 10) -> str:
        token = secrets.token_urlsafe(12)
        await d1.execute(
            self.db,
            "INSERT INTO pending_actions (token, chat_id, action, expires_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            [token, chat_id, action, f"+{ttl_minutes} minutes"])
        return token

    async def consume_pending(self, token: str, chat_id: int) -> str | None:
        """Atomically mark a confirmation used; returns its action if valid, else None."""
        row = await d1.first(
            self.db,
            "UPDATE pending_actions SET used_at = datetime('now') WHERE token = ? AND chat_id = ? "
            "AND used_at IS NULL AND expires_at > datetime('now') RETURNING action",
            [token, chat_id])
        return row["action"] if row else None

    # --- history deletion -------------------------------------------------------------
    async def delete_history(self, chat_id: int) -> list[str]:
        """Delete all conversations + messages for a chat. Returns their LangGraph thread ids."""
        rows = await d1.query(self.db, "SELECT id, thread_id FROM conversations WHERE chat_id = ?", [chat_id])
        await d1.execute(
            self.db, "DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE chat_id = ?)",
            [chat_id])
        await d1.execute(self.db, "DELETE FROM conversations WHERE chat_id = ?", [chat_id])
        return [r["thread_id"] for r in rows]
