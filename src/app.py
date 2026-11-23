"""Process one Telegram update end to end."""

from __future__ import annotations

import html
import logging
import secrets
from typing import Any

from langchain_core.messages import HumanMessage

from config import Config
from graph.builder import RECURSION_LIMIT, build_graph
from graph.state import Deps
from llm.gemini import GeminiClient
from media.download import MediaError, download
from media.transcribe import transcribe
from memory.checkpointer import D1CheckpointSaver
from storage.repo import Repo
from telegram.client import TelegramClient
from telegram.format import render_chunks
from telegram.parser import Inbound, parse_update

log = logging.getLogger("assistant")

HELP_EN = (
    "<b>Personal assistant</b>\n"
    "Write to me in English or Farsi, send a voice message, or share a photo.\n"
    "Say \"remember that…\" to save something to long-term memory.\n\n"
    "/new — start a fresh conversation\n"
    "/memory — show what I remember about you\n"
    "/forget — delete memories (<code>/forget 3</code> deletes one)\n"
    "/deletehistory — permanently delete our chat history\n"
    "/whoami — show your Telegram user ID\n"
    "/help — this message"
)
BOT_COMMANDS = [("new", "Start a fresh conversation"), ("memory", "What I remember about you"),
                ("forget", "Delete memories"), ("deletehistory", "Delete chat history"),
                ("help", "How to use the assistant"), ("whoami", "Show your Telegram user ID")]
BOT_COMMANDS_FA = [("new", "شروع گفتگوی جدید"), ("memory", "آنچه درباره شما به خاطر دارم"),
                   ("forget", "حذف خاطره‌ها"), ("deletehistory", "حذف تاریخچه گفتگو"),
                   ("help", "راهنما"), ("whoami", "نمایش شناسه تلگرام")]
CONFIRM_TEXT = {
    "delete_history": "🗑️ Permanently delete <b>all</b> chat history (messages and saved conversation state)?\n"
                      "Your saved memories and preferences are kept (use /forget for those).",
    "forget_all": "🧠 Permanently delete <b>all</b> saved memories and preferences?",
}
UNSUPPORTED = "Sorry, I can read text, voice messages, audio files and photos — not this kind of message yet."
NO_SPEECH = "🎙️ I couldn't make out any speech in that voice message. Could you try again or type it?"


class Services:
    def __init__(self, cfg: Config, db: Any, http: Any):
        self.cfg = cfg
        self.http = http
        self.repo = Repo(db)
        self.telegram = TelegramClient(cfg.telegram_bot_token, http)
        self.llm = GeminiClient(cfg.gemini_api_key, http)
        self.saver = D1CheckpointSaver(db)
        self.graph = build_graph(self.saver)


async def _reply(svc: Services, chat_id: int, markdown: str, reply_to: int | None = None,
                 prefix_html: str = "") -> int | None:
    last_id = None
    for i, chunk in enumerate(render_chunks(markdown)):
        text = (prefix_html + chunk) if i == 0 else chunk
        plain = markdown if len(markdown) < 3500 else None
        last_id = await svc.telegram.send_message(chat_id, text, plain_fallback=plain,
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
    if msg.kind == "callback":
        return await _callback(msg, svc)
    if msg.kind == "unsupported":
        await svc.telegram.send_message(msg.chat_id, UNSUPPORTED)
        return "unsupported"

    await svc.telegram.send_typing(msg.chat_id)
    usage: list[dict] = []
    attachments: list[dict] = []
    prefix_html = ""
    try:
        if msg.kind == "text":
            human_text = msg.text
        else:
            try:
                media = await download(msg, svc.telegram, svc.http, svc.cfg.telegram_bot_token,
                                       svc.cfg.max_media_bytes)
            except MediaError as exc:
                await svc.telegram.send_message(msg.chat_id, html.escape(str(exc), quote=False))
                return f"rejected:{msg.kind}"
            if msg.kind == "voice":
                transcript, t_usage = await transcribe(svc.llm, svc.cfg.model_chain("transcribe"), media)
                usage.append(t_usage)
                if not transcript:
                    await svc.telegram.send_message(msg.chat_id, NO_SPEECH)
                    return "voice:empty"
                human_text = f"[Voice message] {transcript}" + (f"\n(caption: {msg.text})" if msg.text else "")
                shown = transcript if len(transcript) <= 400 else transcript[:400] + "…"
                prefix_html = f"🎙️ <i>{html.escape(shown)}</i>\n\n"
                await svc.telegram.send_typing(msg.chat_id)
            else:  # photo
                attachments = [media.gemini_part()]
                human_text = f"[Photo] {msg.text}".strip()

        answer, conv = await _run_graph(msg, svc, human_text, attachments, usage)
    finally:
        for u in usage:
            await svc.repo.add_usage(msg.update_id, u)

    await svc.repo.add_message(conv["id"], "user", msg.kind, human_text, update_id=msg.update_id,
                               telegram_message_id=msg.message_id)
    sent_id = await _reply(svc, msg.chat_id, answer, prefix_html=prefix_html)
    await svc.repo.add_message(conv["id"], "assistant", "text", answer, update_id=msg.update_id,
                               telegram_message_id=sent_id)
    return f"answered:{msg.kind}"


async def _run_graph(msg: Inbound, svc: Services, human_text: str, attachments: list[dict],
                     usage: list[dict]) -> tuple[str, dict]:
    conv = await svc.repo.active_conversation(msg.chat_id, msg.user_id)
    deps = Deps(
        llm=svc.llm,
        models=svc.cfg.models,
        repo=svc.repo,
        user_id=msg.user_id,
        update_id=msg.update_id,
        default_timezone=svc.cfg.timezone,
        context_turns=svc.cfg.context_turns,
        max_state_messages=svc.cfg.max_state_messages,
        run_id=f"u{msg.update_id}",
        attachments=attachments,
        usage=usage,
    )
    result = await svc.graph.ainvoke(
        {"messages": [HumanMessage(human_text)], "current_input": msg.to_state(), "tool_call_count": 0},
        {"configurable": {"thread_id": conv["thread_id"]}, "recursion_limit": RECURSION_LIMIT},
        context=deps,
    )
    return result["final_response"], conv


async def _command(msg: Inbound, svc: Services) -> str:
    cmd, tg = msg.command, svc.telegram
    if cmd in ("start", "help"):
        await tg.send_message(msg.chat_id, HELP_EN)
    elif cmd in ("new", "reset"):
        await svc.repo.new_conversation(msg.chat_id, msg.user_id)
        await tg.send_message(msg.chat_id, "🆕 Started a fresh conversation. (Your saved memories are kept.)")
    elif cmd == "whoami":
        await tg.send_message(msg.chat_id, f"Your Telegram user ID is <code>{msg.user_id}</code>.")
    elif cmd == "memory":
        await tg.send_message(msg.chat_id, _memory_html(await svc.repo.profile(msg.user_id)))
    elif cmd == "forget" and msg.text.strip().lstrip("#").isdigit():
        fact_id = int(msg.text.strip().lstrip("#"))
        removed = await svc.repo.delete_fact(msg.user_id, fact_id)
        await tg.send_message(msg.chat_id, f"🗑️ Deleted memory #{fact_id}." if removed
                              else f"I couldn't find memory #{fact_id}. See /memory.")
    elif cmd in ("forget", "deletehistory"):
        action = "forget_all" if cmd == "forget" else "delete_history"
        token = await svc.repo.create_pending(msg.chat_id, action)
        await tg.send_message(msg.chat_id, CONFIRM_TEXT[action] + "\n<i>This button expires in 10 minutes.</i>",
                              buttons=[[("Yes, delete", f"ok:{token}"), ("Cancel", f"no:{token}")]])
    else:
        await tg.send_message(msg.chat_id, "Unknown command. Try /help.")
    return f"command:{cmd}"


def _memory_html(profile: dict) -> str:
    prefs, facts = profile["preferences"], profile["facts"]
    if not prefs and not facts:
        return "🧠 I don't have any saved memories or preferences yet.\nTell me \"remember that…\" to save one."
    lines = ["🧠 <b>What I remember</b>"]
    if prefs:
        lines.append("\n<b>Preferences</b>")
        lines += [f"• {html.escape(k)}: {html.escape(v)}" for k, v in prefs.items()]
    if facts:
        lines.append("\n<b>Memories</b>")
        lines += [f"<code>#{f['id']}</code> {html.escape(f['content'])}" for f in facts]
        lines.append("\n<i>Delete one with /forget &lt;number&gt;, or everything with /forget.</i>")
    return "\n".join(lines)


async def _callback(msg: Inbound, svc: Services) -> str:
    verb, _, token = msg.text.partition(":")
    action = await svc.repo.consume_pending(token, msg.chat_id) if token else None
    tg = svc.telegram
    if action is None:
        await tg.answer_callback(msg.callback_id or "", "This confirmation has expired or was already used.")
        await tg.edit_message(msg.chat_id, msg.message_id, "⌛ This confirmation is no longer valid.")
        return "callback:invalid"
    if verb != "ok":
        await tg.answer_callback(msg.callback_id or "", "Cancelled")
        await tg.edit_message(msg.chat_id, msg.message_id, "Cancelled — nothing was deleted.")
        return f"callback:cancel:{action}"

    if action == "delete_history":
        threads = await svc.repo.delete_history(msg.chat_id)
        for thread_id in threads:
            await svc.saver.adelete_thread(thread_id)
        done = f"🗑️ Deleted {len(threads)} conversation(s) and their saved state."
    elif action == "forget_all":
        await svc.repo.forget_everything(msg.user_id)
        done = "🧠 All saved memories and preferences were deleted."
    else:
        done = "Nothing to do."
    await tg.answer_callback(msg.callback_id or "", "Done")
    await tg.edit_message(msg.chat_id, msg.message_id, done)
    return f"callback:{action}"
