"""Planner tools exposed to the model (registered only when the planner is enabled)."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from planner import values as v
from planner.google_auth import GoogleAuthError
from planner.service import PlannerError
from planner.sheets import SheetsError
from tools.registry import Tool, ToolContext

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


PLANNER_TOOLS: list[Tool] = [
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
