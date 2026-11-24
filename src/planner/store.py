"""Read planner tabs into objects. One store per message: everything read is cached for that message."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from planner import values as v
from planner.model import Event, Goal, Routine, Settings, Task
from planner.schema import (GOALCALC, GOALS, HABITS, HABITS_FIRST_DATE, HABITS_HEADER_RANGE, ROUTINES,
                            SCHEDULE, SETTINGS_KEYS_RANGE, SETTINGS_LISTS_RANGE, TASKS, TabDef)
from planner.sheets import SheetsClient

log = logging.getLogger("assistant")

PARSERS = {TASKS.name: Task.parse, SCHEDULE.name: Event.parse, GOALS.name: Goal.parse, ROUTINES.name: Routine.parse}


def pad(row: list[Any], width: int) -> list[Any]:
    """The API omits trailing empty cells: pad every row to the tab's width."""
    return list(row) + [None] * (width - len(row)) if len(row) < width else list(row[:width])


class PlannerStore:
    def __init__(self, sheets: SheetsClient):
        self.sheets = sheets
        self._settings: Settings | None = None
        self._tabs: dict[str, list[Any]] = {}
        self._habit_names: list[str] | None = None

    # --- settings ---------------------------------------------------------------------
    async def settings(self) -> Settings:
        if self._settings is None:
            got = await self.sheets.batch_get([SETTINGS_KEYS_RANGE, SETTINGS_LISTS_RANGE, HABITS_HEADER_RANGE])
            self._settings = Settings.parse(got[SETTINGS_KEYS_RANGE], got[SETTINGS_LISTS_RANGE])
            self._set_habit_header(got[HABITS_HEADER_RANGE])
        return self._settings

    def _set_habit_header(self, rows: list[list[Any]]) -> None:
        header = pad(rows[0] if rows else [], len(HABITS.columns))
        self._habit_names = [v.to_text(x) for x in header[2:10]]  # C..J; "(unused)" marks empty slots

    async def habit_names(self) -> list[str]:
        """Habit name per slot C..J (empty string for unused slots)."""
        if self._habit_names is None:
            await self.settings()
        return ["" if n in ("", "(unused)") else n for n in (self._habit_names or [])]

    # --- data tabs ----------------------------------------------------------------------
    async def load_many(self, tabs: list[TabDef]) -> None:
        missing = [t for t in tabs if t.name not in self._tabs]
        if not missing:
            return
        ranges = [t.data_range for t in missing]
        if self._settings is None:  # piggy-back settings on the same request
            ranges += [SETTINGS_KEYS_RANGE, SETTINGS_LISTS_RANGE, HABITS_HEADER_RANGE]
        got = await self.sheets.batch_get(ranges)
        if self._settings is None:
            self._settings = Settings.parse(got[SETTINGS_KEYS_RANGE], got[SETTINGS_LISTS_RANGE])
            self._set_habit_header(got[HABITS_HEADER_RANGE])
        for tab in missing:
            self._tabs[tab.name] = self._parse(tab, got[tab.data_range])

    def _parse(self, tab: TabDef, rows: list[list[Any]]) -> list[Any]:
        parser = PARSERS[tab.name]
        items, seen = [], set()
        for i, raw in enumerate(rows):
            row = pad(raw, len(tab.columns))
            item_id = v.to_text(row[0])
            if not item_id:
                continue
            if item_id in seen:
                log.warning("planner: duplicate ID in %s", tab.name)
                continue
            seen.add(item_id)
            items.append(parser(i + 2, row))
        return items

    async def load(self, tab: TabDef) -> list[Any]:
        await self.load_many([tab])
        return self._tabs[tab.name]

    def invalidate(self, tab_name: str) -> None:
        self._tabs.pop(tab_name, None)

    # --- habits -------------------------------------------------------------------------
    @staticmethod
    def habit_row_for(day: dt.date) -> int:
        return 2 + (day - dt.date(*HABITS_FIRST_DATE)).days

    async def habits_for(self, start: dt.date, end: dt.date) -> dict[dt.date, dict[str, bool | None]]:
        """Habit values per date in [start, end] (dates outside the pre-filled range are skipped)."""
        names = await self.habit_names()
        first, last = max(self.habit_row_for(start), 2), min(self.habit_row_for(end), HABITS.max_row)
        if first > last:
            return {}
        rng = HABITS.a1(f"A{first}:J{last}")
        rows = (await self.sheets.batch_get([rng]))[rng]
        out: dict[dt.date, dict[str, bool | None]] = {}
        for raw in rows:
            row = pad(raw, 10)
            day = v.to_date(row[0])
            if day is None:
                continue
            out[day] = {n: v.to_bool(row[2 + i]) for i, n in enumerate(names) if n}
        return out

    # --- goals ----------------------------------------------------------------------------
    async def goal_progress(self) -> dict[str, float | None]:
        rng = GOALCALC.a1("A2:I301")
        rows = (await self.sheets.batch_get([rng]))[rng]
        out: dict[str, float | None] = {}
        for raw in rows:
            row = pad(raw, 9)
            gid = v.to_text(row[0])
            if gid:
                out[gid] = v.to_number(row[8])
        return out
