"""Typed planner objects parsed from sheet rows (row lists already padded to width)."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from planner import values as v
from planner.schema import GOALS, ROUTINES, SCHEDULE, TASKS, TabDef

log = logging.getLogger("assistant")

DEFAULT_LISTS: dict[str, list[str]] = {
    "Areas": ["Work", "Health", "Money", "Family", "Personal growth", "Home", "Fun"],
    "TaskStatus": ["Not started", "In progress", "Waiting", "Done", "Cancelled"],
    "EventStatus": ["Planned", "Done", "Skipped", "Cancelled"],
    "Priority": ["P1", "P2", "P3", "P4"],
    "EventType": ["Event", "Task block", "Routine", "Focus", "Break", "Travel"],
    "Flexibility": ["Fixed", "Flexible"],
    "GoalLevel": ["Year", "Quarter", "Month", "Week"],
    "GoalStatus": ["Active", "Done", "Paused", "Dropped"],
    "Habits": [],
    "ReviewType": ["Daily", "Weekly", "Monthly", "Yearly"],
    "Actor": ["me", "ai", "sample"],
    "InboxStatus": ["New", "Processed", "Discarded"],
    "YesNo": ["Yes", "No"],
}


def _range_minutes(text: str, default: tuple[int, int]) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*", v.normalize_digits(text or ""))
    if not m:
        return default
    return v.parse_hhmm(m.group(1)), v.parse_hhmm(m.group(2))


@dataclass
class Settings:
    planning_year: int = 2027
    timezone: str = "America/Chicago"
    day_start: int = 6 * 60
    day_end: int = 1440
    slot_minutes: int = 30
    work_hours: tuple[int, int] = (9 * 60, 17 * 60)
    max_focus_minutes: int = 4 * 60
    buffer_minutes: int = 10
    quiet_hours: tuple[int, int] = (23 * 60, 6 * 60)
    lists: dict[str, list[str]] = field(default_factory=lambda: {k: list(x) for k, x in DEFAULT_LISTS.items()})

    @classmethod
    def parse(cls, keys_rows: list[list[Any]], list_rows: list[list[Any]]) -> Settings:
        s = cls()
        kv = {v.to_text(r[0]): (r[1] if len(r) > 1 else None) for r in keys_rows if r and r[0]}
        try:
            s.planning_year = v.to_int(kv.get("PlanningYear")) or s.planning_year
            s.timezone = v.to_text(kv.get("Timezone")) or s.timezone
            s.day_start = v.to_minutes(kv.get("DayStart")) if kv.get("DayStart") not in (None, "") else s.day_start
            s.day_end = v.to_minutes(kv.get("DayEnd")) or s.day_end
            s.slot_minutes = v.to_int(kv.get("SlotMinutes")) or s.slot_minutes
            s.work_hours = _range_minutes(v.to_text(kv.get("WorkHours")), s.work_hours)
            focus = v.to_number(kv.get("MaxFocusHoursPerDay"))
            s.max_focus_minutes = int(focus * 60) if focus else s.max_focus_minutes
            buffer = v.to_int(kv.get("BufferMinutes"))
            s.buffer_minutes = buffer if buffer is not None else s.buffer_minutes
            s.quiet_hours = _range_minutes(v.to_text(kv.get("QuietHours")), s.quiet_hours)
        except Exception:  # malformed settings: keep defaults
            log.warning("planner: could not parse Settings; using defaults")
            s = cls()
        # Lists: header in row 1 (E1:R1), values below, stop at the first empty cell.
        if list_rows:
            header = list_rows[0]
            for col, name in enumerate(header):
                name = v.to_text(name)
                if name not in DEFAULT_LISTS:
                    continue
                values: list[str] = []
                rows = list_rows[1:9] if name == "Habits" else list_rows[1:]  # Habits: N2:N9
                for row in rows:
                    cell = v.to_text(row[col]) if col < len(row) else ""
                    if not cell:
                        if name == "Habits":
                            continue  # habit slots may have gaps
                        break
                    values.append(cell)
                if values or name == "Habits":
                    s.lists[name] = values
        return s


def _get(row: list[Any], tab: TabDef, header: str) -> Any:
    i = tab.idx(header)
    return row[i] if i < len(row) else None


@dataclass
class Task:
    row: int
    id: str
    title: str
    area: str
    goal_id: str
    priority: str
    status: str
    due: dt.date | None
    planned: dt.date | None
    est_minutes: int | None
    flexibility: str
    notes: str
    created_by: str
    completed: dt.date | None

    @classmethod
    def parse(cls, row_no: int, r: list[Any]) -> Task:
        g = lambda h: _get(r, TASKS, h)  # noqa: E731
        return cls(row_no, v.to_text(g("TaskID")), v.to_text(g("Title")), v.to_text(g("Area")),
                   v.to_text(g("GoalID")), v.to_text(g("Priority")) or "P2", v.to_text(g("Status")) or "Not started",
                   v.to_date(g("DueDate")), v.to_date(g("PlannedDate")), v.to_int(g("EstMinutes")),
                   v.to_text(g("Flexibility")) or "Flexible", v.to_text(g("Notes")), v.to_text(g("CreatedBy")),
                   v.to_date(g("CompletedDate")))

    @property
    def open(self) -> bool:
        return self.status not in ("Done", "Cancelled")


@dataclass
class Event:
    row: int
    id: str
    date: dt.date | None
    start: int | None
    end: int | None
    title: str
    type: str
    area: str
    linked_id: str
    flexibility: str
    status: str
    location: str
    notes: str
    created_by: str

    @classmethod
    def parse(cls, row_no: int, r: list[Any]) -> Event:
        g = lambda h: _get(r, SCHEDULE, h)  # noqa: E731
        return cls(row_no, v.to_text(g("EventID")), v.to_date(g("Date")), v.to_minutes(g("Start")),
                   v.to_minutes(g("End")), v.to_text(g("Title")), v.to_text(g("Type")), v.to_text(g("Area")),
                   v.to_text(g("LinkedID")), v.to_text(g("Flexibility")) or "Flexible",
                   v.to_text(g("Status")) or "Planned", v.to_text(g("Location")), v.to_text(g("Notes")),
                   v.to_text(g("CreatedBy")))

    @property
    def active(self) -> bool:
        """Counts for overlaps and agendas."""
        return self.status not in ("Cancelled", "Skipped") and self.start is not None and self.end is not None

    @property
    def minutes(self) -> int:
        return (self.end or 0) - (self.start or 0)


@dataclass
class Goal:
    row: int
    id: str
    title: str
    level: str
    area: str
    parent: str
    start: dt.date | None
    end: dt.date | None
    metric: str
    target: float | None
    current: float | None
    status: str
    notes: str
    created_by: str

    @classmethod
    def parse(cls, row_no: int, r: list[Any]) -> Goal:
        g = lambda h: _get(r, GOALS, h)  # noqa: E731
        return cls(row_no, v.to_text(g("GoalID")), v.to_text(g("Title")), v.to_text(g("Level")), v.to_text(g("Area")),
                   v.to_text(g("ParentGoalID")), v.to_date(g("StartDate")), v.to_date(g("EndDate")),
                   v.to_text(g("Metric")), v.to_number(g("Target")), v.to_number(g("Current")),
                   v.to_text(g("Status")) or "Active", v.to_text(g("Why / Notes")), v.to_text(g("CreatedBy")))


@dataclass
class Routine:
    row: int
    id: str
    title: str
    area: str
    days: str
    window_start: int | None
    window_end: int | None
    duration: int | None
    flexibility: str
    type: str
    active: bool

    @classmethod
    def parse(cls, row_no: int, r: list[Any]) -> Routine:
        g = lambda h: _get(r, ROUTINES, h)  # noqa: E731
        return cls(row_no, v.to_text(g("RoutineID")), v.to_text(g("Title")), v.to_text(g("Area")),
                   v.to_text(g("Days")), v.to_minutes(g("WindowStart")), v.to_minutes(g("WindowEnd")),
                   v.to_int(g("DurationMin")), v.to_text(g("Flexibility")) or "Flexible",
                   v.to_text(g("Type")) or "Routine", v.to_text(g("Active")) == "Yes")
