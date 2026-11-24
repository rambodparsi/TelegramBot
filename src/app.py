"""Process one Telegram update end to end."""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from config import Config
from graph.builder import RECURSION_LIMIT, build_graph
from graph.state import Deps
from llm.gemini import GeminiClient, GeminiError
from media.download import MediaError, download
from media.transcribe import transcribe
from memory.checkpointer import D1CheckpointSaver
from storage.repo import Repo
from planner.google_auth import GoogleAuthError, ServiceAccountAuth
from planner.service import PlannerService
from planner.sheets import SheetsClient, SheetsError
from planner.store import PlannerStore
from planner.tools import PLANNER_TOOLS
from storage.d1 import D1Error
from tools import registry
from telegram.client import TelegramClient, TelegramError
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
    "/status — usage and health\n"
    "/whoami — show your Telegram user ID\n"
    "/help — this message"
)
BOT_COMMANDS = [("new", "Start a fresh conversation"), ("memory", "What I remember about you"),
                ("forget", "Delete memories"), ("deletehistory", "Delete chat history"),
                ("status", "Usage and health"), ("help", "How to use the assistant"),
                ("whoami", "Show your Telegram user ID")]
BOT_COMMANDS_FA = [("new", "شروع گفتگوی جدید"), ("memory", "آنچه درباره شما به خاطر دارم"),
                   ("forget", "حذف خاطره‌ها"), ("deletehistory", "حذف تاریخچه گفتگو"),
                   ("status", "وضعیت و مصرف"), ("help", "راهنما"), ("whoami", "نمایش شناسه تلگرام")]
CONFIRM_TEXT = {
    "delete_history": "🗑️ Permanently delete <b>all</b> chat history (messages and saved conversation state)?\n"
                      "Your saved memories and preferences are kept (use /forget for those).",
    "forget_all": "🧠 Permanently delete <b>all</b> saved memories and preferences?",
}
UNSUPPORTED = "Sorry, I can read text, voice messages, audio files and photos — not this kind of message yet."
NO_SPEECH = "🎙️ I couldn't make out any speech in that voice message. Could you try again or type it?"


RETRY_DELAYS = (10, 30, 90)  # seconds before queue redelivery, per attempt


class RetryLater(Exception):
    """Ask the queue to redeliver this update after ``delay`` seconds."""

    def __init__(self, delay: int):
        super().__init__(f"retry in {delay}s")
        self.delay = delay


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, GeminiError):
        return exc.retryable
    if isinstance(exc, TelegramError):
        return exc.status == 429 or exc.status >= 500
    if isinstance(exc, (SheetsError, GoogleAuthError)):
        return exc.retryable
    return isinstance(exc, (D1Error, OSError, ConnectionError, TimeoutError))


class Services:
    def __init__(self, cfg: Config, db: Any, http: Any):
        self.cfg = cfg
        self.http = http
        self.repo = Repo(db)
        self.telegram = TelegramClient(cfg.telegram_bot_token, http)
        self.llm = GeminiClient(cfg.gemini_api_key, http)
        self.saver = D1CheckpointSaver(db)
        self.graph = build_graph(self.saver)
        # Planner (Google Sheets): long-lived client so the OAuth token is cached per isolate.
        self.sheets: SheetsClient | None = None
        if cfg.planner_enabled:
            try:
                auth = ServiceAccountAuth(cfg.google_service_account_json or "", http)
                self.sheets = SheetsClient(cfg.planner_sheet_id or "", auth, http)
            except GoogleAuthError as exc:
                log.error("planner disabled: %s", exc)
        self.tools = registry.tool_map(PLANNER_TOOLS if self.sheets else None)
        self.clock = None  # tests may set a callable returning the owner's local "now"

    def planner_for(self, timezone: str) -> PlannerService | None:
        """A fresh PlannerService (with its own per-message read cache), or None when disabled."""
        if self.sheets is None:
            return None
        try:
            tz = ZoneInfo(timezone)
        except Exception:
            tz = ZoneInfo(self.cfg.timezone)
        now = self.clock() if self.clock else dt.datetime.now(tz).replace(tzinfo=None, second=0, microsecond=0)
        service = PlannerService(PlannerStore(self.sheets), self.repo, now)
        service.fixed_clock = self.clock is not None

        async def ask_confirmation(pending: dict, ctx: Any) -> None:
            token = await self.repo.create_pending(ctx.chat_id, "planner_confirm", 30, pending)
            if ctx.buttons is not None:
                ctx.buttons.append([("✅ Confirm", f"ok:{token}"), ("✖️ Cancel", f"no:{token}")])

        service.on_confirm = ask_confirmation
        return service

    def is_owner(self, user_id: int) -> bool:
        return bool(self.cfg.authorized_user_ids) and user_id == min(self.cfg.authorized_user_ids)


async def _reply(svc: Services, chat_id: int, markdown: str, reply_to: int | None = None,
                 prefix_html: str = "", buttons: list | None = None) -> int | None:
    last_id = None
    chunks = render_chunks(markdown)
    for i, chunk in enumerate(chunks):
        text = (prefix_html + chunk) if i == 0 else chunk
        plain = markdown if len(markdown) < 3500 else None
        last_id = await svc.telegram.send_message(chat_id, text, plain_fallback=plain,
                                                  reply_to=reply_to if i == 0 else None,
                                                  buttons=buttons if i == len(chunks) - 1 else None)
    return last_id


async def handle_update(update: dict[str, Any], svc: Services, attempt: int = 1, max_attempts: int = 1) -> str:
    """Process one update. Returns a short status string (for logs/tests).

    Raises ``RetryLater`` for temporary failures when another attempt is allowed
    (only in queue mode, where ``max_attempts`` > 1).
    """
    msg = parse_update(update)
    if msg is None:
        return "ignored:type"
    cfg = svc.cfg

    if msg.user_id not in cfg.authorized_user_ids:
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

    claim = await svc.repo.claim_update(msg.update_id, msg.chat_id)
    if claim is None:
        return "ignored:duplicate"
    attempt = max(attempt, int(claim.get("attempts") or 1))

    try:
        status = await _process(msg, svc, claim)
        await svc.repo.finish_update(msg.update_id, "done")
        return status
    except Exception as exc:
        ref = secrets.token_hex(3)
        detail = f"[{ref}] {type(exc).__name__}: {exc}"
        if is_retryable(exc) and attempt < max_attempts:
            log.warning("update %s attempt %s will retry: %s", msg.update_id, attempt, detail)
            await svc.repo.finish_update(msg.update_id, "retry", detail)
            raise RetryLater(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]) from exc
        log.error("update %s failed: %s", msg.update_id, detail)
        await svc.repo.finish_update(msg.update_id, "failed", detail)
        try:
            busy = isinstance(exc, GeminiError) and exc.status in (429, 503)
            text = ("⏳ The AI service is busy or out of free quota right now. Please try again in a few minutes."
                    if busy else "⚠️ Sorry, something went wrong.")
            await svc.telegram.send_message(msg.chat_id, f"{text} (ref <code>{ref}</code>)")
        except Exception:
            pass
        return f"failed:{ref}"


async def _process(msg: Inbound, svc: Services, claim: dict | None = None) -> str:
    if msg.kind == "command":
        return await _command(msg, svc)
    if msg.kind == "callback":
        return await _callback(msg, svc)
    if msg.kind == "unsupported":
        await svc.telegram.send_message(msg.chat_id, UNSUPPORTED)
        return "unsupported"

    if claim and claim.get("response"):
        # A previous attempt already generated the reply; deliver it without re-running the model.
        stored = claim.get("response_buttons")
        return await _deliver(msg, svc, claim["conversation_id"], claim.get("kind") or msg.kind,
                              claim.get("user_text") or "", claim["response"], claim.get("response_prefix") or "",
                              [[tuple(b) for b in row] for row in json.loads(stored)] if stored else None)

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

        retry = bool(claim and int(claim.get("attempts") or 1) > 1)
        answer, conv, buttons = await _run_graph(msg, svc, human_text, attachments, usage, retry=retry)
    finally:
        for u in usage:
            await svc.repo.add_usage(msg.update_id, u)

    await svc.repo.save_response(msg.update_id, conv["id"], msg.kind, human_text, answer, prefix_html, buttons)
    return await _deliver(msg, svc, conv["id"], msg.kind, human_text, answer, prefix_html, buttons)


async def _deliver(msg: Inbound, svc: Services, conversation_id: str, kind: str, human_text: str,
                   answer: str, prefix_html: str, buttons: list | None = None) -> str:
    sent_id = await _reply(svc, msg.chat_id, answer, prefix_html=prefix_html, buttons=buttons)
    await svc.repo.add_message(conversation_id, "user", kind, human_text, update_id=msg.update_id,
                               telegram_message_id=msg.message_id)
    await svc.repo.add_message(conversation_id, "assistant", "text", answer, update_id=msg.update_id,
                               telegram_message_id=sent_id)
    return f"answered:{kind}"


async def _repair_thread(svc: Services, config: dict) -> None:
    """After a mid-turn failure, drop the unfinished turn so the retry starts from a clean history.

    A crash between a tool call and the final answer leaves a HumanMessage and/or an AIMessage with
    unanswered tool calls in the checkpoint; replaying the message on top of that would duplicate the
    turn and send Gemini a function call without a response.
    """
    snap = await svc.graph.aget_state(config)
    messages = (snap.values or {}).get("messages", []) if snap else []
    last_done = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage) and not m.tool_calls), default=-1)
    tail = [m for m in messages[last_done + 1:] if m.id]
    if tail:
        await svc.graph.aupdate_state(config, {"messages": [RemoveMessage(id=m.id) for m in tail]}, as_node="finalize")


async def _run_graph(msg: Inbound, svc: Services, human_text: str, attachments: list[dict],
                     usage: list[dict], retry: bool = False) -> tuple[str, dict, list]:
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
        http=svc.http,
        tavily_api_key=svc.cfg.tavily_api_key,
        search_provider=svc.cfg.search_provider,
        tools=svc.tools,
        # The planner belongs to the owner only (smallest authorized ID); other users don't get it.
        planner=svc.planner_for(svc.cfg.timezone) if svc.is_owner(msg.user_id) else None,
        chat_id=msg.chat_id,
    )
    if deps.planner is None and svc.sheets is not None:
        deps.tools = registry.tool_map()
    if retry:
        await _repair_thread(svc, {"configurable": {"thread_id": conv["thread_id"]}})
    result = await svc.graph.ainvoke(
        {"messages": [HumanMessage(human_text)], "current_input": msg.to_state(), "tool_call_count": 0},
        {"configurable": {"thread_id": conv["thread_id"]}, "recursion_limit": RECURSION_LIMIT},
        context=deps,
    )
    buttons = list(deps.buttons)
    if deps.undo_ops:
        token = await svc.repo.create_pending(msg.chat_id, "planner_undo", 120, {"op_keys": deps.undo_ops})
        buttons.append([("↩️ Undo", f"ok:{token}")])
    return result["final_response"], conv, buttons


async def _command(msg: Inbound, svc: Services) -> str:
    cmd, tg = msg.command, svc.telegram
    if cmd in ("start", "help"):
        await tg.send_message(msg.chat_id, HELP_EN)
    elif cmd in ("new", "reset"):
        await svc.repo.new_conversation(msg.chat_id, msg.user_id)
        await tg.send_message(msg.chat_id, "🆕 Started a fresh conversation. (Your saved memories are kept.)")
    elif cmd == "whoami":
        await tg.send_message(msg.chat_id, f"Your Telegram user ID is <code>{msg.user_id}</code>.")
    elif cmd == "status":
        await tg.send_message(msg.chat_id, _status_html(svc, await svc.repo.status_summary(msg.user_id)))
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


def _status_html(svc: Services, s: dict) -> str:
    cfg = svc.cfg
    lines = [f"📊 <b>Status</b> — {html.escape(cfg.app_env)}", "", "<b>Last 24 hours</b>"]
    by_status = {r["status"]: r for r in s["updates"]}
    total = sum(int(r["n"]) for r in s["updates"])
    retries = sum(int(r.get("retries") or 0) for r in s["updates"])
    lines.append(f"• Messages: {total} (done {int(by_status.get('done', {}).get('n', 0))}, "
                 f"failed {int(by_status.get('failed', {}).get('n', 0))}, retries {retries})")
    tools = s["tools"]
    lines.append(f"• Tool calls: {int(tools.get('n') or 0)} ({int(tools.get('bad') or 0)} rejected/failed)")
    if s["usage"]:
        lines.append("\n<b>Gemini usage (24h)</b>")
        for u in s["usage"]:
            fb = f", {int(u['fb'])} fallbacks" if u.get("fb") else ""
            lines.append(f"• {html.escape(u['role'])} · <code>{html.escape(str(u['model']))}</code>: "
                         f"{int(u['calls'])} calls, {int(u['tin']):,} in / {int(u['tout']):,} out tokens{fb}")
    mem = s["memory"]
    lines += ["", f"🧠 {int(mem.get('facts') or 0)} memories, {int(mem.get('prefs') or 0)} preferences",
              f"🤖 Chat models: <code>{html.escape(' → '.join(cfg.model_chain('chat')))}</code>",
              "<i>Free tier: Google limits requests per model per minute and per day.</i>"]
    return "\n".join(lines)


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


async def _planner_callback(msg: Inbound, svc: Services, verb: str, token: str, action: str,
                            payload: dict | None) -> str:
    tg = svc.telegram
    await tg.remove_buttons(msg.chat_id, msg.message_id)  # keep the reply text, drop its buttons
    planner = svc.planner_for(svc.cfg.timezone) if svc.is_owner(msg.user_id) else None
    if planner is None:
        await tg.answer_callback(msg.callback_id or "", "The planner isn't available.")
        return "callback:planner_unavailable"
    if action == "planner_confirm" and verb != "ok":
        await tg.answer_callback(msg.callback_id or "", "Cancelled")
        await tg.send_message(msg.chat_id, "Cancelled. Nothing was changed.")
        return "callback:cancel:planner_confirm"
    op_key = f"cb:{token}"
    if action == "planner_confirm":
        result = await planner.execute_confirmed(payload or {}, op_key)
    else:
        result = await planner.undo((payload or {}).get("op_keys") or [], op_key)
    ok = result.get("ok")
    text = ("✅ " if ok else "⚠️ ") + html.escape(result.get("summary") or result.get("message") or "Done.", quote=False)
    buttons = None
    if action == "planner_confirm" and ok and result.get("changed"):
        undo_token = await svc.repo.create_pending(msg.chat_id, "planner_undo", 120, {"op_keys": [op_key]})
        buttons = [[("↩️ Undo", f"ok:{undo_token}")]]
    await tg.answer_callback(msg.callback_id or "", "Done" if ok else "Not done")
    await tg.send_message(msg.chat_id, text, buttons=buttons)
    return f"callback:{action}:{'ok' if ok else result.get('error', 'failed')}"


async def _callback(msg: Inbound, svc: Services) -> str:
    verb, _, token = msg.text.partition(":")
    pending = await svc.repo.consume_pending_full(token, msg.chat_id) if token else None
    action = pending["action"] if pending else None
    tg = svc.telegram
    if action in ("planner_confirm", "planner_undo"):
        return await _planner_callback(msg, svc, verb, token, action, pending["payload"])
    if action is None:
        await tg.answer_callback(msg.callback_id or "", "This button has expired or was already used.")
        await tg.remove_buttons(msg.chat_id, msg.message_id)  # keep the message text
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
