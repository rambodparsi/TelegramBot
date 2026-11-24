"""PlannerService: the high-level planner operations used by tools (one instance per message)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from planner import rules
from planner import values as v
from planner.model import Event, Goal, Task
from planner.schema import GOALS, SCHEDULE, TASKS
from planner.store import PlannerStore

MAX_AGENDA_DAYS = 14
_PRIO = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


class PlannerError(Exception):
    """A domain error returned to the model as {"ok": false, "error": code, "message": ...}."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def result(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


def day_label(day: dt.date) -> str:
    return f"{day.strftime('%a %b')} {day.day}"


def _event_json(e: Event) -> dict[str, Any]:
    out = {"id": e.id, "start": v.fmt_minutes(e.start), "end": v.fmt_minutes(e.end), "title": e.title,
           "type": e.type, "area": e.area, "flexibility": e.flexibility, "status": e.status}
    if e.linked_id:
        out["linked_id"] = e.linked_id
    if e.location:
        out["location"] = e.location
    return out


def _task_json(t: Task) -> dict[str, Any]:
    out: dict[str, Any] = {"id": t.id, "title": t.title, "area": t.area, "priority": t.priority, "status": t.status}
    if t.due:
        out["due"] = t.due.isoformat()
    if t.planned:
        out["planned"] = t.planned.isoformat()
    if t.est_minutes:
        out["est_minutes"] = t.est_minutes
    return out


class PlannerService:
    def __init__(self, store: PlannerStore, repo: Any, now: dt.datetime):
        self.store = store
        self.repo = repo
        self.now = now            # owner's local time (naive)
        self.today = now.date()
        self.fixed_clock = False  # tests pin "now"; don't re-anchor it

    def set_timezone(self, tz_name: str) -> None:
        """Re-anchor 'now' to the owner's saved timezone preference (called once per message)."""
        from zoneinfo import ZoneInfo

        if self.fixed_clock:
            return
        try:
            self.now = dt.datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None, second=0, microsecond=0)
            self.today = self.now.date()
        except Exception:
            pass

    # --- read operations -------------------------------------------------------------------
    async def get_agenda(self, start: dt.date, end: dt.date | None = None) -> dict[str, Any]:
        end = end or start
        if end < start:
            raise PlannerError("invalid_argument", "end_date is before start_date")
        if (end - start).days + 1 > MAX_AGENDA_DAYS:
            raise PlannerError("invalid_argument", f"agenda range is limited to {MAX_AGENDA_DAYS} days")
        await self.store.load_many([SCHEDULE, TASKS])
        events: list[Event] = await self.store.load(SCHEDULE)
        tasks: list[Task] = await self.store.load(TASKS)
        habits = await self.store.habits_for(start, end)

        days = []
        totals = {"blocks": 0, "tasks": 0, "done": 0}
        day = start
        while day <= end:
            day_events = sorted((e for e in events if e.date == day and e.status != "Cancelled"
                                 and e.start is not None), key=lambda e: (e.start, e.end or 0))
            day_tasks = [t for t in tasks if t.status != "Cancelled"
                         and (t.planned == day or (t.planned is None and t.due == day))]
            day_tasks.sort(key=lambda t: (t.status == "Done", _PRIO.get(t.priority, 9)))
            entry: dict[str, Any] = {"date": day.isoformat(), "weekday": day.strftime("%A"),
                                     "events": [_event_json(e) for e in day_events],
                                     "tasks": [_task_json(t) for t in day_tasks]}
            if day in habits:
                entry["habits"] = habits[day]
            days.append(entry)
            totals["blocks"] += len(day_events)
            totals["tasks"] += len(day_tasks)
            totals["done"] += sum(t.status == "Done" for t in day_tasks)
            day += dt.timedelta(days=1)

        overdue = sorted((t for t in tasks if t.open and t.due and t.due < self.today),
                         key=lambda t: (t.due, _PRIO.get(t.priority, 9)))[:10]
        span = day_label(start) if start == end else f"{day_label(start)} – {day_label(end)}"
        summary = (f"{span}: {totals['blocks']} blocks, {totals['tasks']} tasks ({totals['done']} done), "
                   f"{len(overdue)} overdue")
        return {"ok": True, "summary": summary, "data": {
            "today": self.today.isoformat(), "days": days, "overdue": [_task_json(t) for t in overdue]}}

    async def find_free_slots(self, day: dt.date, minutes: int, area: str | None = None,
                              earliest: int | None = None, latest: int | None = None) -> dict[str, Any]:
        settings = await self.store.settings()
        if area and area not in settings.lists["Areas"]:
            raise PlannerError("invalid_argument", "unknown area; use one of: " + ", ".join(settings.lists["Areas"]))
        events = await self.store.load(SCHEDULE)
        slots = rules.find_free_slots(events, day, minutes, area, settings, self.now, earliest, latest, limit=5)
        return {"ok": True,
                "summary": f"{len(slots)} free slot(s) of {minutes} min on {day_label(day)}",
                "data": {"date": day.isoformat(),
                         "slots": [{"start": v.fmt_minutes(s), "end": v.fmt_minutes(e)} for s, e in slots]}}

    async def goals(self, level: str | None = None, year: int | None = None,
                    include_inactive: bool = False) -> dict[str, Any]:
        settings = await self.store.settings()
        if level and level not in settings.lists["GoalLevel"]:
            raise PlannerError("invalid_argument", "level must be one of: " + ", ".join(settings.lists["GoalLevel"]))
        goals: list[Goal] = await self.store.load(GOALS)
        progress = await self.store.goal_progress()
        if year is None and level in ("Year", "Quarter"):
            year = settings.planning_year
        out = []
        for g in goals:
            if level and g.level != level:
                continue
            if year and (g.end is None or g.end.year != year):
                continue
            if not include_inactive and g.status in ("Dropped", "Paused"):
                continue
            pct = progress.get(g.id)
            item: dict[str, Any] = {"id": g.id, "title": g.title, "level": g.level, "area": g.area,
                                    "status": g.status,
                                    "progress_pct": None if pct is None else round(min(pct, 1) * 100)}
            for key, value in (("parent", g.parent), ("start", g.start), ("end", g.end), ("metric", g.metric),
                               ("target", g.target), ("current", g.current)):
                if value not in (None, ""):
                    item[key] = value.isoformat() if isinstance(value, dt.date) else value
            out.append(item)
        scope = " ".join(x for x in (level or "all", str(year) if year else "") if x)
        return {"ok": True, "summary": f"{len(out)} goal(s) ({scope})", "data": {"goals": out[:40]}}
