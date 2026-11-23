"""Fetch Telegram media and prepare it for Gemini (validated, base64, never stored)."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from http_client import Http
from telegram.client import TelegramClient
from telegram.parser import Inbound

# Telegram bots can download at most 20 MB; Gemini inline requests are capped at ~20 MB
# total, and base64 grows data by a third. Our own cap is configurable and lower.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
AUDIO_TYPES = {"audio/ogg", "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/aac",
               "audio/flac", "audio/aiff", "audio/mp4", "audio/x-m4a", "audio/m4a"}
# Gemini names for a few aliases Telegram may report.
_MIME_ALIASES = {"audio/mp3": "audio/mpeg", "audio/x-wav": "audio/wav", "audio/x-m4a": "audio/mp4",
                 "audio/m4a": "audio/mp4", "audio/opus": "audio/ogg"}


class MediaError(Exception):
    """User-facing problem with an attachment (message is safe to show)."""


@dataclass
class Attachment:
    mime_type: str
    data_b64: str
    size: int

    def gemini_part(self) -> dict:
        return {"inline_data": {"mime_type": self.mime_type, "data": self.data_b64}}


def check_media(msg: Inbound, max_bytes: int) -> str:
    """Validate type and declared size before downloading. Returns the Gemini mime type."""
    mime = _MIME_ALIASES.get((msg.mime_type or "").lower(), (msg.mime_type or "").lower())
    allowed = IMAGE_TYPES if msg.kind == "photo" else AUDIO_TYPES
    if mime not in allowed:
        raise MediaError(f"Sorry, I can't read this file type ({msg.mime_type or 'unknown'}).")
    if msg.file_size and msg.file_size > min(max_bytes, TELEGRAM_DOWNLOAD_LIMIT):
        raise MediaError(f"That file is too large ({msg.file_size / 1_048_576:.1f} MB). "
                         f"The limit is {max_bytes / 1_048_576:.0f} MB.")
    return mime


async def download(msg: Inbound, telegram: TelegramClient, http: Http, token: str, max_bytes: int) -> Attachment:
    mime = check_media(msg, max_bytes)
    info = await telegram.call("getFile", {"file_id": msg.file_id})
    path = info.get("file_path") if isinstance(info, dict) else None
    if not path:
        raise MediaError("Telegram didn't let me download that file. Please try sending it again.")
    resp = await http.get_bytes(f"https://api.telegram.org/file/bot{token}/{path}")
    if resp.status != 200:
        raise RuntimeError(f"file download failed: HTTP {resp.status}")
    if len(resp.content) > max_bytes:
        raise MediaError("That file is too large.")
    if not resp.content:
        raise MediaError("That file was empty.")
    return Attachment(mime_type=mime, data_b64=base64.b64encode(resp.content).decode("ascii"), size=len(resp.content))
