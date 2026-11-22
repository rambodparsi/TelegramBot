"""Telegram Bot API client (only the methods the bot uses)."""

from __future__ import annotations

from typing import Any

from http_client import Http


class TelegramError(RuntimeError):
    def __init__(self, method: str, status: int, description: str):
        super().__init__(f"{method} failed ({status}): {description}")
        self.status = status
        self.description = description


class TelegramClient:
    def __init__(self, token: str, http: Http):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._http = http

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        resp = await self._http.post_json(self._base + method, payload)
        try:
            data = resp.json() or {}
        except ValueError:
            data = {}
        if resp.status != 200 or not data.get("ok"):
            raise TelegramError(method, resp.status, str(data.get("description", resp.text[:200])))
        return data.get("result")

    async def send_message(self, chat_id: int, html_text: str, *, plain_fallback: str | None = None,
                           reply_to: int | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": html_text, "parse_mode": "HTML",
                                   "link_preview_options": {"is_disabled": True}}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            result = await self.call("sendMessage", payload)
        except TelegramError as exc:
            if exc.status != 400 or plain_fallback is None:
                raise
            # Formatting rejected ("can't parse entities"): resend as plain text.
            payload.pop("parse_mode")
            payload["text"] = plain_fallback
            result = await self.call("sendMessage", payload)
        return int(result["message_id"])

    async def send_typing(self, chat_id: int) -> None:
        try:
            await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            pass  # cosmetic only

    async def set_webhook(self, url: str, secret: str) -> Any:
        return await self.call("setWebhook", {
            "url": url, "secret_token": secret, "allowed_updates": ["message", "callback_query"],
            "max_connections": 10, "drop_pending_updates": False,
        })

    async def get_webhook_info(self) -> Any:
        return await self.call("getWebhookInfo", {})

    async def set_my_commands(self, commands: list[tuple[str, str]], language_code: str | None = None) -> Any:
        payload: dict[str, Any] = {"commands": [{"command": c, "description": d} for c, d in commands]}
        if language_code:
            payload["language_code"] = language_code
        return await self.call("setMyCommands", payload)
