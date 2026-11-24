"""Morning brief and evening check-in (deterministic text, no LLM call). Telegram HTML."""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

MAX_EVENT_LINES = 8
MAX_TASKS = 5
LOW_PRIORITY_TYPES = ("Routine", "Break")


def _e(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _short_date(iso: str) -> str:
    day = dt.date.fromisoformat(iso)
    return f"{day.strftime('%b')} {day.day}"


def _event_line(e: dict[str, Any]) -> str:
    when = e["start"] if e["type"] in LOW_PRIORITY_TYPES and e["end"] == e["start"] else f"{e['start']}–{e['end']}"
    return f"{when} {_e(e['title'])}"


def _pick_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Up to 8 events in time order; routines and breaks are dropped first when there are more."""
    live = [e for e in events if e["status"] not in ("Cancelled", "Skipped")]
    if len(live) <= MAX_EVENT_LINES:
        return live, 0
    keep = [e for e in live if e["type"] not in LOW_PRIORITY_TYPES][:MAX_EVENT_LINES]
    room = MAX_EVENT_LINES - len(keep)
    keep += [e for e in live if e["type"] in LOW_PRIORITY_TYPES][:room]
    keep.sort(key=lambda e: e["start"])
    return keep, len(live) - len(keep)


def morning_brief(agenda: dict[str, Any]) -> tuple[str, str]:
    """Returns (html, plain) for the owner's day."""
    day = agenda["data"]["days"][0]
    date = dt.date.fromisoformat(day["date"])
    lines = [f"☀️ <b>{date.strftime('%A')}, {date.strftime('%b')} {date.day}</b>"]
    events, more = _pick_events(day["events"])
    if events:
        lines.append("<b>Today</b>")
        lines += [_event_line(e) for e in events]
        if more:
            lines.append(f"+{more} more")
    else:
        lines.append("Nothing on the calendar today.")
    todo = [t for t in day["tasks"] if t["status"] != "Done"]
    if todo:
        shown = " · ".join(f"{_e(t['title'])} ({t['priority']})" for t in todo[:MAX_TASKS])
        extra = f" · +{len(todo) - MAX_TASKS} more" if len(todo) > MAX_TASKS else ""
        lines.append(f"<b>To-do</b> ({len(todo)}): {shown}{extra}")
    overdue = agenda["data"].get("overdue") or []
    if overdue:
        shown = " · ".join(f"{_e(t['title'])} (due {_short_date(t['due'])})" for t in overdue[:3])
        extra = f" · +{len(overdue) - 3} more" if len(overdue) > 3 else ""
        lines.append(f"⚠️ Overdue: {shown}{extra}")
    habits = day.get("habits") or {}
    if habits:
        lines.append("<b>Habits</b>: " + " · ".join(_e(h) for h in habits))
    text = "\n".join(lines)
    return text, _plain(text)


def evening_checkin(agenda: dict[str, Any]) -> tuple[str, str]:
    day = agenda["data"]["days"][0]
    tasks = day["tasks"]
    done = [t for t in tasks if t["status"] == "Done"]
    open_ = [t for t in tasks if t["status"] != "Done"]
    lines = [f"🌙 <b>Shutdown</b> — {len(done)} of {len(tasks)} tasks done today." if tasks
             else "🌙 <b>Shutdown</b> — no tasks were planned today."]
    if open_:
        shown = " · ".join(_e(t["title"]) for t in open_[:MAX_TASKS])
        extra = f" · +{len(open_) - MAX_TASKS} more" if len(open_) > MAX_TASKS else ""
        lines.append(f"Still open: {shown}{extra}")
        lines.append('Reply e.g. "move the rest to tomorrow" or "done with ' + _e(open_[0]["title"].lower()) + '".')
    missing = [h for h, value in (day.get("habits") or {}).items() if value is None]
    if missing:
        lines.append("Habits not logged yet: " + ", ".join(_e(h) for h in missing))
    text = "\n".join(lines)
    return text, _plain(text)


def _plain(text: str) -> str:
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", text))
