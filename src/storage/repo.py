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
