"""Process one Telegram update end to end."""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage

from config import Config
from graph.builder import build_graph
from graph.state import Deps
from llm.gemini import GeminiClient
from memory.checkpointer import D1CheckpointSaver
from prompts import MAIN_ASSISTANT
from storage.repo import Repo
from telegram.client import TelegramClient
from telegram.format import render_chunks
from telegram.parser import Inbound, parse_update

log = logging.getLogger("assistant")

HELP_EN = (
    "<b>Personal assistant</b>\n"
    "Just write to me — in English or Farsi.\n\n"
    "/new — start a fresh conversation\n"
    "/help — this message\n"
    "/whoami — show your Telegram user ID"
)
COMING_SOON = {
    "voice": "🎙️ Voice messages are coming soon. For now, please type your message.",
    "photo": "🖼️ Photo understanding is coming soon. For now, please describe it in text.",
    "unsupported": "Sorry, I can only read text messages for now.",
}


class Services:
    def __init__(self, cfg: Config, db: Any, http: Any):
        self.cfg = cfg
        self.repo = Repo(db)
        self.telegram = TelegramClient(cfg.telegram_bot_token, http)
        self.llm = GeminiClient(cfg.gemini_api_key, http)
        self.graph = build_graph(D1CheckpointSaver(db))


def _system_prompt(cfg: Config) -> str:
    try:
        now = dt.datetime.now(ZoneInfo(cfg.timezone))
    except Exception:
        now = dt.datetime.now(dt.timezone.utc)
    return MAIN_ASSISTANT.format(timezone=cfg.timezone, now=now.strftime("%A, %B %d, %Y %H:%M"))


async def _reply(svc: Services, chat_id: int, markdown: str, reply_to: int | None = None) -> int | None:
    last_id = None
    for i, chunk in enumerate(render_chunks(markdown)):
        last_id = await svc.telegram.send_message(chat_id, chunk, plain_fallback=markdown if len(markdown) < 4000 else None,
                                                  reply_to=reply_to if i == 0 else None)
    return last_id


async def handle_update(update: dict[str, Any], svc: Services) -> str:
    """Returns a short status string (for logs/tests)."""
    msg = parse_update(update)
    if msg is None:
        return "ignored:type"
    cfg = svc.cfg

    allowed = msg.user_id in cfg.authorized_user_ids
    if not allowed:
        # Before the allowlist is configured, answer /whoami so the owner can find their ID.
        if not cfg.authorized_user_ids and msg.kind == "command" and msg.command in ("whoami", "start"):
            await svc.telegram.send_message(
                msg.chat_id,
                f"Your Telegram user ID is <code>{msg.user_id}</code>.\n"
                "Add it as the <code>AUTHORIZED_TELEGRAM_USER_IDS</code> secret in Cloudflare to unlock the assistant.",
            )
            return "whoami:unconfigured"
        return "ignored:unauthorized"
    if msg.chat_type != "private":
        return "ignored:chat_type"

    if not await svc.repo.claim_update(msg.update_id, msg.chat_id):
        return "ignored:duplicate"

    try:
        status = await _process(msg, svc)
        await svc.repo.finish_update(msg.update_id, "done")
        return status
    except Exception as exc:
        ref = secrets.token_hex(3)
        log.error("update %s failed [%s]: %s: %s", msg.update_id, ref, type(exc).__name__, exc)
        await svc.repo.finish_update(msg.update_id, "failed", f"[{ref}] {type(exc).__name__}: {exc}")
        try:
            await svc.telegram.send_message(msg.chat_id, f"⚠️ Sorry, something went wrong. (ref <code>{ref}</code>)")
        except Exception:
            pass
        return f"failed:{ref}"


async def _process(msg: Inbound, svc: Services) -> str:
    if msg.kind == "command":
        return await _command(msg, svc)
    if msg.kind in COMING_SOON:
        await svc.telegram.send_message(msg.chat_id, COMING_SOON[msg.kind])
        return f"unsupported:{msg.kind}"

    await svc.telegram.send_typing(msg.chat_id)
    conv = await svc.repo.active_conversation(msg.chat_id, msg.user_id)
    deps = Deps(
        llm=svc.llm,
        models=svc.cfg.models,
        system_prompt=_system_prompt(svc.cfg),
        context_turns=svc.cfg.context_turns,
        max_state_messages=svc.cfg.max_state_messages,
        run_id=f"u{msg.update_id}",
    )
    try:
        result = await svc.graph.ainvoke(
            {"messages": [HumanMessage(msg.text)], "current_input": msg.to_state(), "tool_call_count": 0},
            {"configurable": {"thread_id": conv["thread_id"]}},
            context=deps,
        )
    finally:
        for u in deps.usage:
            await svc.repo.add_usage(msg.update_id, u)

    answer = result["final_response"]
    await svc.repo.add_message(conv["id"], "user", "text", msg.text, update_id=msg.update_id,
                               telegram_message_id=msg.message_id)
    sent_id = await _reply(svc, msg.chat_id, answer)
    await svc.repo.add_message(conv["id"], "assistant", "text", answer, update_id=msg.update_id,
                               telegram_message_id=sent_id)
    return "answered"


async def _command(msg: Inbound, svc: Services) -> str:
    cmd = msg.command
    if cmd in ("start", "help"):
        await svc.telegram.send_message(msg.chat_id, HELP_EN)
    elif cmd == "new":
        await svc.repo.new_conversation(msg.chat_id, msg.user_id)
        await svc.telegram.send_message(msg.chat_id, "🆕 Started a fresh conversation.")
    elif cmd == "whoami":
        await svc.telegram.send_message(msg.chat_id, f"Your Telegram user ID is <code>{msg.user_id}</code>.")
    else:
        await svc.telegram.send_message(msg.chat_id, "Unknown command. Try /help.")
    return f"command:{cmd}"
