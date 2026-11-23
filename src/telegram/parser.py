"""Normalize Telegram updates into a small, typed structure."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Inbound:
    update_id: int
    chat_id: int
    chat_type: str
    user_id: int
    message_id: int
    kind: str                     # text|command|voice|photo|unsupported
    text: str = ""                # text, caption, or command arguments
    command: str | None = None    # e.g. "new" for /new
    file_id: str | None = None
    file_unique_id: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    language_code: str | None = None

    def to_state(self) -> dict[str, Any]:
        return asdict(self)


def parse_update(update: dict[str, Any]) -> Inbound | None:
    """Return a normalized message, or None for update types the bot ignores."""
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    if "id" not in chat or "id" not in sender or sender.get("is_bot"):
        return None

    base = dict(
        update_id=int(update["update_id"]),
        chat_id=int(chat["id"]),
        chat_type=str(chat.get("type", "")),
        user_id=int(sender["id"]),
        message_id=int(msg.get("message_id", 0)),
        language_code=sender.get("language_code"),
    )

    text = msg.get("text")
    if isinstance(text, str):
        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            command = head[1:].split("@", 1)[0].lower()
            return Inbound(kind="command", command=command, text=rest.strip(), **base)
        return Inbound(kind="text", text=text, **base)

    caption = msg.get("caption") or ""
    if isinstance(msg.get("voice"), dict):
        v = msg["voice"]
        return Inbound(kind="voice", text=caption, file_id=v.get("file_id"), file_unique_id=v.get("file_unique_id"),
                       mime_type=v.get("mime_type", "audio/ogg"), file_size=v.get("file_size"), **base)
    if isinstance(msg.get("audio"), dict):
        a = msg["audio"]
        return Inbound(kind="voice", text=caption, file_id=a.get("file_id"), file_unique_id=a.get("file_unique_id"),
                       mime_type=a.get("mime_type"), file_size=a.get("file_size"), **base)
    if isinstance(msg.get("photo"), list) and msg["photo"]:
        p = max(msg["photo"], key=lambda s: s.get("file_size") or s.get("width", 0) * s.get("height", 0))
        return Inbound(kind="photo", text=caption, file_id=p.get("file_id"), file_unique_id=p.get("file_unique_id"),
                       mime_type="image/jpeg", file_size=p.get("file_size"), **base)
    doc = msg.get("document")
    if isinstance(doc, dict) and str(doc.get("mime_type", "")).startswith("image/"):
        return Inbound(kind="photo", text=caption, file_id=doc.get("file_id"), file_unique_id=doc.get("file_unique_id"),
                       mime_type=doc.get("mime_type"), file_size=doc.get("file_size"), **base)
    return Inbound(kind="unsupported", text=caption, **base)
