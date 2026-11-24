"""PlannerService: the high-level planner operations used by tools (one instance per message)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from planner import rules
from planner import values as v
import json
import uuid

from planner.model import Event, Goal, Task
from planner.schema import CHANGELOG, GOALS, HABITS, INBOX, SCHEDULE, TASKS
from planner.store import PlannerStore
from planner.writes import HM, PlannerWriteError, WriteBatch

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


def when_label(day: dt.date, start: int, end: int) -> str:
    return f"{day_label(day)} {v.fmt_minutes(start)}–{v.fmt_minutes(end)}"


SLEEP_WORDS = ("sleep", "bed", "wind", "خواب")


class PlannerService:
    def __init__(self, store: PlannerStore, repo: Any, now: dt.datetime):
        self.store = store
        self.repo = repo
        self.now = now            # owner's local time (naive)
        self.today = now.date()
        self.fixed_clock = False  # tests pin "now"; don't re-anchor it
        self.on_confirm = None    # async (pending_payload, tool_ctx) -> None; set by the app (Phase 3)

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


    # =========================================================================================
    # Writes
    # =========================================================================================

    # --- retry safety ---------------------------------------------------------------------------
    async def run_op(self, op_key: str, fn) -> dict[str, Any]:
        """Run a write op at most once per op key (queue retries replay the stored result)."""
        existing = await self.repo.get_op(op_key) if self.repo else None
        if existing and existing["status"] == "done" and existing.get("result_json"):
            result = json.loads(existing["result_json"])
            result["replayed"] = True
            return result
        if existing and existing["status"] == "started":
            landed = await self._changelog_items(op_key)
            if landed:
                result = {"ok": True, "summary": "Already done.", "changed": landed}
                await self.repo.finish_op(op_key, result)
                return result
        if self.repo:
            await self.repo.start_op(op_key)
        try:
            result = await fn()
        except PlannerError as exc:
            result = exc.result()
        except PlannerWriteError as exc:
            result = {"ok": False, "error": exc.code, "message": exc.message}
        # Retryable errors (Sheets/Google outages) propagate: the op stays 'started'.
        if self.repo and not result.get("needs_confirmation"):
            await self.repo.finish_op(op_key, result)
        return result

    async def _changelog_items(self, op_key: str) -> list[str]:
        rng = CHANGELOG.a1(f"E2:J{CHANGELOG.max_row}")
        rows = (await self.store.sheets.batch_get([rng]))[rng]
        return list(dict.fromkeys(v.to_text(r[0]) for r in rows if len(r) >= 6 and v.to_text(r[5]) == op_key))

    async def _commit(self, batch: WriteBatch):
        return await batch.commit(self.store.sheets, self.store)

    @staticmethod
    def new_op_key() -> str:
        return f"x:{uuid.uuid4().hex[:16]}"

    # --- shared checks ------------------------------------------------------------------------
    def _check_date(self, day: dt.date, name: str = "date") -> None:
        if not (self.today - dt.timedelta(days=366) <= day <= self.today + dt.timedelta(days=730)):
            raise PlannerError("invalid_argument", f"{name} is too far in the past or future")

    async def _check_enum(self, list_name: str, value: str, arg: str) -> None:
        allowed = (await self.store.settings()).lists[list_name]
        if value not in allowed:
            raise PlannerError("invalid_argument", f"{arg} must be one of: {', '.join(allowed)}")

    async def _task(self, task_id: str) -> Task:
        task = next((t for t in await self.store.load(TASKS) if t.id == task_id), None)
        if task is None:
            raise PlannerError("not_found", f"No to-do with ID {task_id}.")
        return task

    async def _event(self, event_id: str) -> Event:
        event = next((e for e in await self.store.load(SCHEDULE) if e.id == event_id), None)
        if event is None:
            raise PlannerError("not_found", f"No calendar item with ID {event_id}.")
        return event

    @staticmethod
    def confirmation(summary: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "needs_confirmation": True, "summary": summary,
                "_pending": {"op": op, "args": args, "confirmed": True, "summary": summary}}

    async def _check_slot(self, day: dt.date, start: int, end: int, type_: str, title: str,
                          exclude_id: str | None = None) -> list[Event]:
        """Time sanity, day bounds and overlap. Returns that day's events."""
        if not (0 <= start < end <= 1440):
            raise PlannerError("invalid_argument", "start must be before end (a block can't cross midnight; "
                                                   "split it into two blocks)")
        if not 5 <= end - start <= 720:
            raise PlannerError("invalid_argument", "a block must last between 5 minutes and 12 hours")
        self._check_date(day)
        settings = await self.store.settings()
        own_night = type_ == "Routine" and any(w in title.lower() for w in SLEEP_WORDS)
        if not own_night and (start < settings.day_start or end > settings.day_end):
            raise PlannerError("outside_hours", f"That's outside your planning hours "
                                                f"({v.fmt_minutes(settings.day_start)}–{v.fmt_minutes(settings.day_end)}).")
        events = await self.store.load(SCHEDULE)
        clash = rules.conflicts(events, day, start, end, exclude_id)
        if clash:
            c = clash[0]
            others = [e for e in events if e.id != exclude_id]
            slots = rules.find_free_slots(others, day, end - start, None, settings, self.now, limit=3)
            raise PlannerError("overlap", f"That overlaps '{c.title}' ({v.fmt_minutes(c.start)}–{v.fmt_minutes(c.end)}).",
                               conflict={"id": c.id, "title": c.title, "start": v.fmt_minutes(c.start),
                                         "end": v.fmt_minutes(c.end), "flexibility": c.flexibility},
                               suggestions=[{"start": v.fmt_minutes(a), "end": v.fmt_minutes(b)} for a, b in slots])
        return events

    async def _focus_over(self, day: dt.date, minutes: int, type_: str, exclude_id: str | None = None) -> str | None:
        if type_ not in rules.FOCUS_TYPES:
            return None
        settings = await self.store.settings()
        total = rules.focus_minutes(await self.store.load(SCHEDULE), day, exclude_id) + minutes
        if total > settings.max_focus_minutes:
            return (f"This would make {total / 60:.1f} h of focused work on {day_label(day)} "
                    f"(limit {settings.max_focus_minutes / 60:g} h).")
        return None

    @staticmethod
    def _ok(summary: str, changed: list[str], warnings: list[str] | None = None, **data: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True, "summary": summary, "changed": changed}
        if data:
            out["data"] = data
        if warnings:
            out["warning"] = " ".join(warnings)
        return out

    # --- to-dos ---------------------------------------------------------------------------------
    async def add_task(self, op_key: str, title: str, area: str, priority: str = "P2",
                       due_date: dt.date | None = None, planned_date: dt.date | None = None,
                       est_minutes: int | None = None, goal_id: str | None = None,
                       notes: str | None = None) -> dict[str, Any]:
        await self.store.load_many([TASKS])
        await self._check_enum("Areas", area, "area")
        await self._check_enum("Priority", priority, "priority")
        for day, name in ((due_date, "due_date"), (planned_date, "planned_date")):
            if day:
                self._check_date(day, name)
        clean_title = v.sanitize_text(title, 120)
        existing = next((t for t in await self.store.load(TASKS) if t.open
                         and t.title.casefold() == clean_title.lstrip("'").casefold() and t.planned == planned_date),
                        None)
        if existing:
            return self._ok(f"Already in your list: '{existing.title}'", [], task_id=existing.id)
        if goal_id and not any(g.id == goal_id for g in await self.store.load(GOALS)):
            raise PlannerError("not_found", f"No goal with ID {goal_id}.")
        batch = WriteBatch(op_key, f"Owner asked to add '{clean_title.lstrip(chr(39))}'", self.now)
        ref = batch.create(TASKS, {"Title": clean_title, "Area": area, "GoalID": goal_id, "Priority": priority,
                                   "Status": "Not started", "DueDate": due_date, "PlannedDate": planned_date,
                                   "EstMinutes": est_minutes, "Flexibility": "Flexible",
                                   "Notes": v.sanitize_text(notes, 500) if notes else None})
        res = await self._commit(batch)
        task_id = res.placeholders[ref]
        when = f", planned {day_label(planned_date)}" if planned_date else ""
        due = f", due {day_label(due_date)}" if due_date else ""
        return self._ok(f"Added to-do '{clean_title.lstrip(chr(39))}' ({priority}{when}{due})", [task_id],
                        res.warnings, task_id=task_id)

    async def update_task(self, op_key: str, task_id: str, confirmed: bool = False, **fields: Any) -> dict[str, Any]:
        task = await self._task(task_id)
        changes: dict[str, Any] = {}
        mapping = {"title": "Title", "area": "Area", "priority": "Priority", "due_date": "DueDate",
                   "planned_date": "PlannedDate", "est_minutes": "EstMinutes", "goal_id": "GoalID",
                   "notes": "Notes", "status": "Status"}
        for key, value in fields.items():
            if value is None or key not in mapping:
                continue
            if key in ("due_date", "planned_date"):
                value = None if value == "none" else value
                if value:
                    self._check_date(value, key)
            elif key == "area":
                await self._check_enum("Areas", value, "area")
            elif key == "priority":
                await self._check_enum("Priority", value, "priority")
            elif key == "status" and value not in ("Not started", "In progress", "Waiting"):
                raise PlannerError("invalid_argument", "use planner_complete_task or planner_cancel for Done/Cancelled")
            elif key == "title":
                value = v.sanitize_text(value, 120)
            elif key == "notes":
                value = v.sanitize_text(value, 500)
            elif key == "goal_id" and not any(g.id == value for g in await self.store.load(GOALS)):
                raise PlannerError("not_found", f"No goal with ID {value}.")
            changes[mapping[key]] = value
        if not changes:
            raise PlannerError("invalid_argument", "nothing to change")
        if task.flexibility == "Fixed" and not confirmed and ({"DueDate", "PlannedDate"} & set(changes)):
            return self.confirmation(f"Change the dates of FIXED to-do '{task.title}'?", "update_task",
                                     {"task_id": task_id, **{k: (x.isoformat() if isinstance(x, dt.date) else x)
                                                             for k, x in fields.items() if x is not None}})
        batch = WriteBatch(op_key, f"Owner asked to update '{task.title}'", self.now)
        batch.update(TASKS, task_id, changes)
        res = await self._commit(batch)
        parts = ", ".join(f"{k} → {v.display(x) or 'none'}" for k, x in changes.items())
        return self._ok(f"Updated '{task.title}': {parts}", [task_id], res.warnings)

    async def complete_task(self, op_key: str, task_id: str) -> dict[str, Any]:
        await self.store.load_many([TASKS, SCHEDULE])
        task = await self._task(task_id)
        if task.status == "Done":
            return self._ok(f"'{task.title}' was already done.", [])
        if task.status == "Cancelled":
            raise PlannerError("invalid_argument", f"'{task.title}' is cancelled.")
        batch = WriteBatch(op_key, f"Owner finished '{task.title}'", self.now)
        batch.update(TASKS, task_id, {"Status": "Done", "CompletedDate": self.today}, "complete")
        done_blocks, dropped = 0, 0
        for e in await self.store.load(SCHEDULE):
            if e.linked_id == task_id and e.status == "Planned" and e.date:
                if e.date <= self.today:
                    batch.update(SCHEDULE, e.id, {"Status": "Done"}, "complete")
                    done_blocks += 1
                else:
                    batch.update(SCHEDULE, e.id, {"Status": "Cancelled"}, "cancel")
                    dropped += 1
        res = await self._commit(batch)
        extra = (f"; {done_blocks} time block(s) marked done" if done_blocks else "") + \
                (f"; {dropped} future block(s) cancelled" if dropped else "")
        return self._ok(f"Marked '{task.title}' done{extra}", res.changed_ids, res.warnings)

    # --- calendar ---------------------------------------------------------------------------------
    async def schedule(self, op_key: str, date: dt.date, start: int | None = None, end: int | None = None,
                       minutes: int | None = None, task_id: str | None = None, title: str | None = None,
                       area: str | None = None, type: str | None = None, flexibility: str | None = None,
                       location: str | None = None, notes: str | None = None,
                       confirmed: bool = False) -> dict[str, Any]:
        await self.store.load_many([SCHEDULE, TASKS])
        task = await self._task(task_id) if task_id else None
        if task is None and not (title and area):
            raise PlannerError("invalid_argument", "title and area are required when no task_id is given")
        if task and task.status in ("Done", "Cancelled"):
            raise PlannerError("invalid_argument", f"'{task.title}' is already {task.status.lower()}.")
        title = title or (task.title if task else "")
        area = area or (task.area if task else "")
        await self._check_enum("Areas", area, "area")
        type_ = type or ("Task block" if task else "Event")
        await self._check_enum("EventType", type_, "type")
        flex = flexibility or ("Fixed" if type_ == "Event" else "Flexible")
        await self._check_enum("Flexibility", flex, "flexibility")
        length = minutes or ((end - start) if (start is not None and end is not None) else None) \
            or (task.est_minutes if task and task.est_minutes else 60)
        if start is None:
            settings = await self.store.settings()
            slots = rules.find_free_slots(await self.store.load(SCHEDULE), date, length, area, settings, self.now,
                                          limit=1)
            if not slots:
                raise PlannerError("overlap", f"No free {length}-minute slot on {day_label(date)}.", suggestions=[])
            start, end = slots[0]
        end = end if end is not None else start + length
        await self._check_slot(date, start, end, type_, title)
        warning = await self._focus_over(date, end - start, type_)
        args = {"date": date.isoformat(), "start": v.fmt_minutes(start), "end": v.fmt_minutes(end),
                "task_id": task_id, "title": title, "area": area, "type": type_, "flexibility": flex,
                "location": location, "notes": notes}
        if warning and not confirmed:
            return self.confirmation(f"{warning} Schedule '{title}' {when_label(date, start, end)} anyway?",
                                     "schedule", {k: x for k, x in args.items() if x is not None})
        batch = WriteBatch(op_key, f"Owner asked to schedule '{title}'", self.now)
        ref = batch.create(SCHEDULE, {"Date": date, "Start": HM(start), "End": HM(end),
                                      "Title": v.sanitize_text(title, 120), "Type": type_, "Area": area,
                                      "LinkedID": task_id, "Flexibility": flex, "Status": "Planned",
                                      "Location": v.sanitize_text(location, 120) if location else None,
                                      "Notes": v.sanitize_text(notes, 500) if notes else None})
        if task and task.planned != date:
            batch.update(TASKS, task.id, {"PlannedDate": date})
        res = await self._commit(batch)
        event_id = res.placeholders[ref]
        return self._ok(f"Scheduled '{title}' {when_label(date, start, end)}", [event_id] + ([task.id] if task else []),
                        res.warnings, event_id=event_id)

    async def move(self, op_key: str, event_id: str, date: dt.date | None = None, start: int | None = None,
                   end: int | None = None, minutes: int | None = None, confirmed: bool = False) -> dict[str, Any]:
        await self.store.load_many([SCHEDULE, TASKS])
        event = await self._event(event_id)
        if event.status == "Cancelled":
            raise PlannerError("invalid_argument", f"'{event.title}' is cancelled.")
        if date is None and start is None and end is None and minutes is None:
            raise PlannerError("invalid_argument", "give a new date, start, end or length")
        new_date = date or event.date
        new_start = start if start is not None else event.start
        if end is not None:
            new_end = end
        elif minutes is not None:
            new_end = new_start + minutes
        else:
            new_end = new_start + event.minutes
        await self._check_slot(new_date, new_start, new_end, event.type, event.title, exclude_id=event.id)
        summary = f"'{event.title}' → {when_label(new_date, new_start, new_end)}"
        if event.flexibility == "Fixed" and not confirmed:
            return self.confirmation(f"Move FIXED {summary}?", "move",
                                     {"event_id": event_id, "date": new_date.isoformat(),
                                      "start": v.fmt_minutes(new_start), "end": v.fmt_minutes(new_end)})
        warning = await self._focus_over(new_date, new_end - new_start, event.type, exclude_id=event.id)
        if warning and not confirmed:
            return self.confirmation(f"{warning} Move {summary} anyway?", "move",
                                     {"event_id": event_id, "date": new_date.isoformat(),
                                      "start": v.fmt_minutes(new_start), "end": v.fmt_minutes(new_end)})
        changes: dict[str, Any] = {}
        if new_date != event.date:
            changes["Date"] = new_date
        if new_start != event.start:
            changes["Start"] = HM(new_start)
        if new_end != event.end:
            changes["End"] = HM(new_end)
        if not changes:
            return self._ok(f"'{event.title}' is already at that time.", [])
        batch = WriteBatch(op_key, f"Owner asked to move '{event.title}'", self.now)
        batch.update(SCHEDULE, event.id, changes, "move")
        if "Date" in changes and event.type == "Task block" and event.linked_id.startswith("T-"):
            linked = next((t for t in await self.store.load(TASKS) if t.id == event.linked_id), None)
            if linked and linked.planned != new_date:
                batch.update(TASKS, linked.id, {"PlannedDate": new_date}, "move")
        res = await self._commit(batch)
        return self._ok(f"Moved {summary}", res.changed_ids, res.warnings)

    async def cancel(self, op_key: str, item_id: str, reason: str | None = None,
                     confirmed: bool = False) -> dict[str, Any]:
        why = v.sanitize_text(reason, 200) if reason else "Owner asked to cancel"
        batch = WriteBatch(op_key, why, self.now)
        prefix = item_id.split("-")[0]
        if prefix == "T":
            await self.store.load_many([TASKS, SCHEDULE])
            task = await self._task(item_id)
            if task.status == "Cancelled":
                return self._ok(f"'{task.title}' was already cancelled.", [])
            if task.flexibility == "Fixed" and not confirmed:
                return self.confirmation(f"Cancel FIXED to-do '{task.title}'?", "cancel",
                                         {"item_id": item_id, "reason": reason})
            batch.update(TASKS, item_id, {"Status": "Cancelled"}, "cancel")
            for e in await self.store.load(SCHEDULE):
                if e.linked_id == item_id and e.status == "Planned" and e.date and e.date >= self.today:
                    batch.update(SCHEDULE, e.id, {"Status": "Cancelled"}, "cancel")
            label = f"to-do '{task.title}'"
        elif prefix == "E":
            event = await self._event(item_id)
            if event.status == "Cancelled":
                return self._ok(f"'{event.title}' was already cancelled.", [])
            if event.flexibility == "Fixed" and not confirmed:
                return self.confirmation(f"Cancel FIXED '{event.title}' ({when_label(event.date, event.start, event.end)})?",
                                         "cancel", {"item_id": item_id, "reason": reason})
            batch.update(SCHEDULE, item_id, {"Status": "Cancelled"}, "cancel")
            label = f"'{event.title}' ({when_label(event.date, event.start, event.end)})"
        elif prefix == "G":
            goal = next((g for g in await self.store.load(GOALS) if g.id == item_id), None)
            if goal is None:
                raise PlannerError("not_found", f"No goal with ID {item_id}.")
            batch.update(GOALS, item_id, {"Status": "Dropped"}, "cancel")
            label = f"goal '{goal.title}' (dropped)"
        elif prefix == "I":
            batch.update(INBOX, item_id, {"Status": "Discarded"}, "cancel")
            label = f"inbox item {item_id} (discarded)"
        else:
            raise PlannerError("invalid_argument", "unknown ID type")
        res = await self._commit(batch)
        return self._ok(f"Cancelled {label}", res.changed_ids, res.warnings)

    # --- habits, inbox, goals ------------------------------------------------------------------------
    async def log_habit(self, op_key: str, habit: str, date: dt.date | None = None,
                        done: bool = True) -> dict[str, Any]:
        day = date or self.today
        names = [n for n in await self.store.habit_names() if n]
        match = next((n for n in names if n.lower() == habit.strip().lower()), None)
        if match is None:
            raise PlannerError("invalid_argument", "Unknown habit. Valid habits: " + ", ".join(names))
        first = dt.date(2026, 11, 1)
        if not first <= day <= first + dt.timedelta(days=HABITS.max_row - 2):
            raise PlannerError("out_of_range", f"The Habits tab has no row for {day.isoformat()}.")
        if day > self.today:
            raise PlannerError("invalid_argument", "can't log a habit for a future day")
        batch = WriteBatch(op_key, f"Owner logged '{match}'", self.now)
        batch.set_habit(day, match, done)
        res = await self._commit(batch)
        return self._ok(f"{'Logged' if done else 'Marked not done'}: {match} ({day_label(day)})", res.changed_ids)

    async def capture(self, op_key: str, text: str) -> dict[str, Any]:
        batch = WriteBatch(op_key, "Owner captured an idea", self.now)
        clean = v.sanitize_text(text, 300)
        ref = batch.create(INBOX, {"Text": clean, "Status": "New"})
        res = await self._commit(batch)
        return self._ok(f"Saved to Inbox: '{clean.lstrip(chr(39))}'", [res.placeholders[ref]], res.warnings)

    async def update_goal(self, op_key: str, goal_id: str, current: float | None = None,
                          status: str | None = None) -> dict[str, Any]:
        if current is None and status is None:
            raise PlannerError("invalid_argument", "give current and/or status")
        goal = next((g for g in await self.store.load(GOALS) if g.id == goal_id), None)
        if goal is None:
            raise PlannerError("not_found", f"No goal with ID {goal_id}.")
        changes: dict[str, Any] = {}
        if current is not None:
            changes["Current"] = int(current) if float(current).is_integer() else current
        if status is not None:
            await self._check_enum("GoalStatus", status, "status")
            changes["Status"] = status
        batch = WriteBatch(op_key, f"Owner updated goal '{goal.title}'", self.now)
        batch.update(GOALS, goal_id, changes)
        res = await self._commit(batch)
        parts = []
        if "Current" in changes:
            target = f" / {v.display(goal.target)}" if goal.target else ""
            parts.append(f"progress {v.display(changes['Current'])}{target} {goal.metric}".rstrip())
        if "Status" in changes:
            parts.append(f"status {status}")
        return self._ok(f"Goal '{goal.title}': " + ", ".join(parts), [goal_id], res.warnings)
