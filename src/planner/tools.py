"""Planner tools exposed to the model (registered only when the planner is enabled)."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from planner import values as v
from planner.google_auth import GoogleAuthError
from planner.service import PlannerError
from planner.sheets import SheetsError
from tools.registry import Tool, ToolContext, args_hash

log = logging.getLogger("assistant")

DATE = {"type": "string", "format": "date", "description": "YYYY-MM-DD"}
TIME = {"type": "string", "format": "time", "description": "HH:MM, 24-hour"}


def _unavailable(exc: Exception) -> dict[str, Any]:
    status = getattr(exc, "status", None)
    if status in (403, 404):
        message = "I can't open the planner sheet. Check that it's shared with the service account (Editor)."
    else:
        message = "The planner sheet is unavailable right now. Try again in a minute."
    return {"ok": False, "error": "sheet_unavailable", "message": message}


async def _call(ctx: ToolContext, fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
    if ctx.planner is None:
        return {"ok": False, "error": "planner_disabled", "message": "The planner is not connected."}
    try:
        return await fn(ctx.planner, *args, **kwargs)
    except PlannerError as exc:
        return exc.result()
    except (SheetsError, GoogleAuthError) as exc:
        log.warning("planner read failed: %s", type(exc).__name__)
        return _unavailable(exc)


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


async def _get_agenda(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async def run(p):
        return await p.get_agenda(_date(args["start_date"]), _date(args.get("end_date")))
    return await _call(ctx, run)


async def _free_slots(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async def run(p):
        return await p.find_free_slots(
            _date(args["date"]), args["minutes"], args.get("area"),
            v.parse_hhmm(args["earliest"]) if args.get("earliest") else None,
            v.parse_hhmm(args["latest"]) if args.get("latest") else None)
    return await _call(ctx, run)


async def _goals(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async def run(p):
        return await p.goals(args.get("level"), args.get("year"), bool(args.get("include_inactive")))
    return await _call(ctx, run)


# --- writes ------------------------------------------------------------------------------------

def op_key_for(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    from planner.service import PlannerService

    if ctx.update_id is None:
        return PlannerService.new_op_key()
    return f"u{ctx.update_id}:{name}:{args_hash(args)}"


async def _write(ctx: ToolContext, name: str, args: dict[str, Any], fn) -> dict[str, Any]:
    """Run a write op once per op key. Sheets/Google outages propagate so the queue retries the message."""
    if ctx.planner is None:
        return {"ok": False, "error": "planner_disabled", "message": "The planner is not connected."}
    planner = ctx.planner
    key = op_key_for(ctx, name, args)
    try:
        result = await planner.run_op(key, lambda: fn(planner, key))
    except (SheetsError, GoogleAuthError) as exc:
        if exc.retryable:
            raise
        log.warning("planner write failed: %s", type(exc).__name__)
        return _unavailable(exc)
    pending = result.pop("_pending", None)
    if result.get("needs_confirmation") and pending is not None and ctx.planner.on_confirm is not None:
        await ctx.planner.on_confirm(pending, ctx)
    if result.get("ok") and result.get("changed") and not result.get("replayed") and ctx.undo_ops is not None:
        ctx.undo_ops.append(key)
    return result


def _hm(value: str | None) -> int | None:
    return v.parse_hhmm(value) if value else None


def _opt_date(value: str | None):
    if value in (None, ""):
        return None
    return "none" if value == "none" else _date(value)


async def _add_task(ctx, args):
    return await _write(ctx, "planner_add_task", args, lambda p, k: p.add_task(
        k, args["title"], args["area"], args.get("priority", "P2"), _date(args.get("due_date")),
        _date(args.get("planned_date")), args.get("est_minutes"), args.get("goal_id"), args.get("notes")))


async def _update_task(ctx, args):
    fields = {k: x for k, x in args.items() if k != "task_id"}
    for key in ("due_date", "planned_date"):
        if key in fields:
            fields[key] = _opt_date(fields[key])
    return await _write(ctx, "planner_update_task", args, lambda p, k: p.update_task(k, args["task_id"], **fields))


async def _complete_task(ctx, args):
    return await _write(ctx, "planner_complete_task", args, lambda p, k: p.complete_task(k, args["task_id"]))


async def _schedule(ctx, args):
    return await _write(ctx, "planner_schedule", args, lambda p, k: p.schedule(
        k, _date(args["date"]), _hm(args.get("start")), _hm(args.get("end")), args.get("minutes"),
        args.get("task_id"), args.get("title"), args.get("area"), args.get("type"), args.get("flexibility"),
        args.get("location"), args.get("notes")))


async def _move(ctx, args):
    return await _write(ctx, "planner_move", args, lambda p, k: p.move(
        k, args["event_id"], _date(args.get("date")), _hm(args.get("start")), _hm(args.get("end")),
        args.get("minutes")))


async def _cancel(ctx, args):
    return await _write(ctx, "planner_cancel", args, lambda p, k: p.cancel(k, args["item_id"], args.get("reason")))


async def _log_habit(ctx, args):
    return await _write(ctx, "planner_log_habit", args, lambda p, k: p.log_habit(
        k, args["habit"], _date(args.get("date")), args.get("done", True)))


async def _capture(ctx, args):
    return await _write(ctx, "planner_capture", args, lambda p, k: p.capture(k, args["text"]))


async def _update_goal(ctx, args):
    return await _write(ctx, "planner_update_goal", args, lambda p, k: p.update_goal(
        k, args["goal_id"], args.get("current"), args.get("status")))


TASK_ID = {"type": "string", "pattern": r"^T-\d{4}$", "description": "e.g. T-0012"}
EVENT_ID = {"type": "string", "pattern": r"^E-\d{4}$", "description": "e.g. E-0040"}
MINUTES = {"type": "integer", "minimum": 5, "maximum": 720}
PRIORITY = {"type": "string", "enum": ["P1", "P2", "P3", "P4"],
            "description": "P1 must, P2 should (default), P3 could, P4 someday"}
AREA = {"type": "string", "description": "Planner area: Work, Health, Money, Family, Personal growth, Home or Fun"}

WRITE_TOOLS: list[Tool] = [
    Tool("planner_add_task",
         "Create a to-do in the planner. Use when the owner wants to remember or do something without a fixed "
         "time. Set planned_date only if the owner named a day.",
         {"type": "object", "properties": {
             "title": {"type": "string", "maxLength": 120, "description": "Short, starts with a verb"},
             "area": AREA, "priority": PRIORITY, "due_date": DATE, "planned_date": DATE,
             "est_minutes": MINUTES, "goal_id": {"type": "string", "pattern": r"^G-\d{3}$"},
             "notes": {"type": "string", "maxLength": 500}},
          "required": ["title", "area"]},
         "write", _add_task),
    Tool("planner_update_task",
         "Change details of an existing to-do (title, area, priority, dates, estimate, goal, notes, or status "
         "In progress/Waiting/Not started). To finish or cancel a task use planner_complete_task or "
         "planner_cancel. Pass \"none\" as a date to clear it.",
         {"type": "object", "properties": {
             "task_id": TASK_ID, "title": {"type": "string", "maxLength": 120}, "area": AREA,
             "priority": PRIORITY,
             "due_date": {**DATE, "allowNone": True}, "planned_date": {**DATE, "allowNone": True},
             "est_minutes": MINUTES, "goal_id": {"type": "string", "pattern": r"^G-\d{3}$"},
             "notes": {"type": "string", "maxLength": 500},
             "status": {"type": "string", "enum": ["Not started", "In progress", "Waiting"]}},
          "required": ["task_id"]},
         "write", _update_task),
    Tool("planner_complete_task",
         "Mark a to-do as done. Also marks its planned time blocks for today or earlier as done.",
         {"type": "object", "properties": {"task_id": TASK_ID}, "required": ["task_id"]},
         "write", _complete_task),
    Tool("planner_schedule",
         "Put something on the calendar at a specific date and time: a time block for a to-do (pass task_id), "
         "a new event or appointment, or a focus block. If start is omitted, the first suitable free slot is "
         "used.",
         {"type": "object", "properties": {
             "date": DATE, "start": TIME, "end": TIME,
             "minutes": {**MINUTES, "description": "Length when end is omitted (default: task estimate or 60)"},
             "task_id": TASK_ID,
             "title": {"type": "string", "maxLength": 120, "description": "Required if no task_id"},
             "area": {**AREA, "description": "Required if no task_id. " + AREA["description"]},
             "type": {"type": "string", "enum": ["Event", "Task block", "Routine", "Focus", "Break", "Travel"]},
             "flexibility": {"type": "string", "enum": ["Fixed", "Flexible"],
                             "description": "Default Fixed for events/appointments, otherwise Flexible"},
             "location": {"type": "string", "maxLength": 120}, "notes": {"type": "string", "maxLength": 500}},
          "required": ["date"]},
         "write", _schedule),
    Tool("planner_move",
         "Move or resize an existing calendar item (event, time block, routine instance) to another date and/or "
         "time. Keeps its length if only a new start is given.",
         {"type": "object", "properties": {"event_id": EVENT_ID, "date": DATE, "start": TIME, "end": TIME,
                                           "minutes": MINUTES},
          "required": ["event_id"]},
         "write", _move),
    Tool("planner_cancel",
         "Cancel a to-do, calendar item, goal or inbox item by its ID. Nothing is deleted; the item is marked "
         "cancelled.",
         {"type": "object", "properties": {
             "item_id": {"type": "string", "pattern": r"^(T-\d{4}|E-\d{4}|G-\d{3}|I-\d{4})$"},
             "reason": {"type": "string", "maxLength": 200}},
          "required": ["item_id"]},
         "write", _cancel),
    Tool("planner_log_habit",
         "Record whether the owner did a habit on a day (default today). Habit names come from the planner's "
         "Settings. For several habits, call this once per habit.",
         {"type": "object", "properties": {"habit": {"type": "string", "maxLength": 60}, "date": DATE,
                                           "done": {"type": "boolean", "description": "default true"}},
          "required": ["habit"]},
         "write", _log_habit),
    Tool("planner_capture",
         "Save a quick idea or note to the planner's Inbox for later sorting. Use when the owner says something "
         "vague like 'sometime I should…' with no clear task, day or goal.",
         {"type": "object", "properties": {"text": {"type": "string", "maxLength": 300}}, "required": ["text"]},
         "write", _capture),
    Tool("planner_update_goal",
         "Update a goal's progress number (current) or status (Active, Done, Paused, Dropped).",
         {"type": "object", "properties": {
             "goal_id": {"type": "string", "pattern": r"^G-\d{3}$"}, "current": {"type": "number"},
             "status": {"type": "string", "enum": ["Active", "Done", "Paused", "Dropped"]}},
          "required": ["goal_id"]},
         "write", _update_goal),
]

PLANNER_TOOLS: list[Tool] = WRITE_TOOLS + [
    Tool("planner_get_agenda",
         "Get the owner's planner for one or more days: time blocks/events, to-dos planned or due on those days, "
         "habit status, and overdue tasks. Call this before answering any question about the owner's schedule "
         "or before changing it.",
         {"type": "object", "properties": {
             "start_date": DATE,
             "end_date": {**DATE, "description": "YYYY-MM-DD; default = start_date; max 14 days"}},
          "required": ["start_date"]},
         "read", _get_agenda),
    Tool("planner_find_free_slots",
         "Find free time on a day for a block of a given length, respecting the owner's work hours, day "
         "start/end and buffer. Use before scheduling when no time was given.",
         {"type": "object", "properties": {
             "date": DATE,
             "minutes": {"type": "integer", "minimum": 5, "maximum": 720},
             "area": {"type": "string", "description": "Planner area, e.g. Work, Health"},
             "earliest": TIME, "latest": TIME},
          "required": ["date", "minutes"]},
         "read", _free_slots),
    Tool("planner_goals",
         "List the owner's goals with progress. Filter by level and/or year.",
         {"type": "object", "properties": {
             "level": {"type": "string", "enum": ["Year", "Quarter", "Month", "Week"]},
             "year": {"type": "integer", "minimum": 2000, "maximum": 2100},
             "include_inactive": {"type": "boolean"}}},
         "read", _goals),
]
