"""WriteBatch: describe sheet changes by item ID; resolve rows and commit in ONE batchUpdate.

Rows are resolved at commit time from a fresh read, so edits the owner made meanwhile can't
make the bot write to the wrong row. Every change gets ChangeLog rows tagged with the op key.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from planner import rules
from planner import values as v
from planner.schema import (CHANGELOG, DATE_COLUMNS, DATETIME_COLUMNS, HABITS, TIME_COLUMNS, TabDef, col_letter)
from planner.sheets import SheetsClient
from planner.store import PlannerStore, pad

CAPACITY_WARNING = 0.9


class HM(int):
    """A time of day in minutes (written as HH:MM; 1440 = 24:00)."""


class PlannerWriteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def cell_value(value: Any) -> Any:
    """Python value -> USER_ENTERED cell content."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, HM):
        return v.fmt_minutes(int(value))
    if isinstance(value, dt.datetime):
        return v.fmt_datetime(value)
    if isinstance(value, dt.date):
        return v.fmt_date(value)
    return value


def read_display(header: str, raw: Any) -> str:
    """Sheet cell (UNFORMATTED) -> display string for the ChangeLog."""
    if raw is None or raw == "":
        return ""
    if header in DATE_COLUMNS:
        day = v.to_date(raw)
        return v.fmt_date(day) if day else v.to_text(raw)
    if header in TIME_COLUMNS:
        minutes = v.to_minutes(raw)
        return v.fmt_minutes(minutes) if minutes is not None else v.to_text(raw)
    if header in DATETIME_COLUMNS:
        moment = v.to_datetime(raw)
        return v.fmt_datetime(moment) if moment else v.to_text(raw)
    return v.display(raw)


def new_display(value: Any) -> str:
    shown = cell_value(value)
    if isinstance(shown, str) and shown.startswith("'"):
        shown = shown[1:]
    return v.display(shown)


@dataclass
class Create:
    tab: TabDef
    values: dict[str, Any]
    placeholder: str


@dataclass
class Update:
    tab: TabDef
    item_id: str
    changes: dict[str, Any]
    action: str


@dataclass
class SetHabit:
    day: dt.date
    habit: str
    value: bool | None
    action: str = "update"


@dataclass
class CommitResult:
    placeholders: dict[str, str] = field(default_factory=dict)
    created_ids: list[str] = field(default_factory=list)
    changed_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    old_values: dict[tuple[str, str], str] = field(default_factory=dict)  # (item_id, field) -> old display


class WriteBatch:
    def __init__(self, op_key: str, reason: str, now_local: dt.datetime, actor: str = "ai"):
        self.op_key = op_key
        self.reason = v.sanitize_text(reason, 200)
        self.now = now_local.replace(second=0, microsecond=0)
        self.actor = actor
        self.ops: list[Create | Update | SetHabit] = []
        self._n = 0

    def create(self, tab: TabDef, values: dict[str, Any]) -> str:
        self._n += 1
        placeholder = f"@{self._n}"
        self.ops.append(Create(tab, dict(values), placeholder))
        return placeholder

    def update(self, tab: TabDef, item_id: str, changes: dict[str, Any], action: str = "update") -> None:
        if changes:
            self.ops.append(Update(tab, item_id, dict(changes), action))

    def set_habit(self, day: dt.date, habit: str, value: bool | None, action: str = "update") -> None:
        self.ops.append(SetHabit(day, habit, value, action))

    def __bool__(self) -> bool:
        return bool(self.ops)

    async def commit(self, sheets: SheetsClient, store: PlannerStore) -> CommitResult:
        result = CommitResult()
        if not self.ops:
            return result
        tabs = {op.tab.name: op.tab for op in self.ops if isinstance(op, (Create, Update))}
        update_tabs = {op.tab.name for op in self.ops if isinstance(op, Update)}
        habit_rows = sorted({PlannerStore.habit_row_for(op.day) for op in self.ops if isinstance(op, SetHabit)})

        ranges = [CHANGELOG.a1("J1"), CHANGELOG.id_range]
        tab_ranges = {name: (t.data_range if name in update_tabs else t.id_range) for name, t in tabs.items()}
        ranges += list(tab_ranges.values())
        habit_ranges = {r: HABITS.a1(f"A{r}:J{r}") for r in habit_rows if 2 <= r <= HABITS.max_row}
        ranges += list(habit_ranges.values())
        got = await sheets.batch_get(ranges)

        header = got[CHANGELOG.a1("J1")]
        if not header or not header[0] or v.to_text(header[0][0]) != "RequestID":
            raise PlannerWriteError("sheet_not_ready",
                                    "The planner sheet isn't ready: type RequestID into cell J1 of the ChangeLog tab.")
        log_next = 2 + _last_filled(got[CHANGELOG.id_range])

        # Index rows per tab by ID.
        rows_by_id: dict[str, dict[str, tuple[int, list[Any]]]] = {}
        next_row: dict[str, int] = {}
        known_ids: dict[str, list[str]] = {}
        for name, tab in tabs.items():
            raw = got[tab_ranges[name]]
            index: dict[str, tuple[int, list[Any]]] = {}
            duplicates: set[str] = set()
            for i, row in enumerate(raw):
                item_id = v.to_text(row[0]) if row else ""
                if not item_id:
                    continue
                if item_id in index:
                    duplicates.add(item_id)
                index[item_id] = (i + 2, pad(row, len(tab.columns)))
            for d in duplicates:
                index[d] = (-1, [])
            rows_by_id[name] = index
            known_ids[name] = list(index)
            next_row[name] = 2 + _last_filled(raw)

        data: list[tuple[str, list[list[Any]]]] = []
        log_rows: list[list[Any]] = []
        stamp = v.fmt_datetime(self.now)

        def log(action: str, tab: str, item_id: str, field_: str, old: str, new: str) -> None:
            # Old/New values are display text ("21:00", "2026-11-24", "TRUE"): keep them as text so
            # Sheets doesn't convert them (Undo reads them back verbatim).
            log_rows.append([stamp, self.actor, action, tab, v.sanitize_text(item_id, 40), field_,
                             _as_text(old), _as_text(new), self.reason, self.op_key])

        for op in self.ops:
            if isinstance(op, Create):
                tab = op.tab
                values = dict(op.values)
                if tab.id_prefix:
                    new_id = rules.next_id(tab.id_prefix, known_ids[tab.name], tab.id_width)
                    known_ids[tab.name].append(new_id)
                    values[tab.columns[0]] = new_id
                else:
                    new_id = ""
                row = next_row[tab.name]
                if row > tab.max_row:
                    raise PlannerWriteError("capacity", f"The {tab.name} tab is full ({tab.max_row} rows).")
                next_row[tab.name] = row + 1
                if (row - 1) / (tab.max_row - 1) >= CAPACITY_WARNING:
                    result.warnings.append(f"The {tab.name} tab is over 90% full.")
                for col, default in (("CreatedBy", self.actor), ("CreatedAt", self.now), ("UpdatedAt", self.now),
                                     ("CapturedAt", self.now)):
                    if col in tab.columns and col not in values:
                        values[col] = default
                line = [cell_value(values.get(c)) for c in tab.columns]
                data.append((tab.a1(f"A{row}:{tab.last_col}{row}"), [line]))
                result.placeholders[op.placeholder] = new_id
                result.created_ids.append(new_id)
                title = values.get("Title") or values.get("Text") or ""
                log("create", tab.name, new_id, "", "", new_display(title))
            elif isinstance(op, Update):
                tab = op.tab
                item_id = result.placeholders.get(op.item_id, op.item_id)
                found = rows_by_id[tab.name].get(item_id)
                if found is None:
                    raise PlannerWriteError("not_found", f"{item_id} was not found in {tab.name}.")
                row, current = found
                if row < 0:
                    raise PlannerWriteError("invalid_argument",
                                            f"{item_id} appears more than once in {tab.name}; please fix the sheet.")
                changes = dict(op.changes)
                if "UpdatedAt" in tab.columns:
                    changes["UpdatedAt"] = self.now
                for header_name, value in changes.items():
                    col = tab.idx(header_name)
                    data.append((tab.a1(f"{col_letter(col)}{row}"), [[cell_value(value)]]))
                    if header_name == "UpdatedAt":
                        continue
                    old = read_display(header_name, current[col])
                    result.old_values[(item_id, header_name)] = old
                    log(op.action, tab.name, item_id, header_name, old, new_display(value))
                if item_id not in result.changed_ids:
                    result.changed_ids.append(item_id)
            else:  # SetHabit
                row = PlannerStore.habit_row_for(op.day)
                raw = got.get(habit_ranges.get(row, ""), [])
                line = pad(raw[0] if raw else [], 10)
                if v.to_date(line[0]) != op.day:
                    raise PlannerWriteError("out_of_range", f"No Habits row for {op.day.isoformat()}.")
                names = await store.habit_names()
                slot = next((i for i, n in enumerate(names) if n and n.lower() == op.habit.lower()), None)
                if slot is None:
                    raise PlannerWriteError("invalid_argument", "Unknown habit.")
                col = 2 + slot
                old = read_display("", line[col])
                data.append((HABITS.a1(f"{col_letter(col)}{row}"), [[cell_value(op.value)]]))
                log(op.action, "Habits", op.day.isoformat(), names[slot], old, new_display(op.value))
                result.changed_ids.append(f"{op.day.isoformat()}:{names[slot]}")

        if log_next + len(log_rows) - 1 > CHANGELOG.max_row:
            raise PlannerWriteError("capacity", "The ChangeLog tab is full.")
        if log_rows:
            data.append((CHANGELOG.a1(f"A{log_next}:J{log_next + len(log_rows) - 1}"), log_rows))
        await sheets.batch_update(data)
        for name in tabs:
            store.invalidate(name)
        return result


def _as_text(value: str) -> str:
    text = v.sanitize_text(value, 500)
    return text if not text or text.startswith("'") else "'" + text


def _last_filled(rows: list[list[Any]]) -> int:
    """Number of the last data row (1-based within the range) whose column A is filled."""
    last = 0
    for i, row in enumerate(rows):
        if row and row[0] not in (None, ""):
            last = i + 1
    return last
