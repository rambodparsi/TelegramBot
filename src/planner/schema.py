"""The Life Planner sheet contract: tab names, column order, ID formats and row limits.

Column lists are verbatim from the sheet (docs/PLANNER_MODULE_SPEC.md section 3).
Never reorder them: the sheet's views depend on exact positions.
"""

from __future__ import annotations

from dataclasses import dataclass


def col_letter(index: int) -> str:
    """0-based column index -> A1 letters."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


@dataclass(frozen=True)
class TabDef:
    name: str
    columns: tuple[str, ...]
    id_prefix: str | None
    id_width: int
    max_row: int

    def col(self, header: str) -> str:
        return col_letter(self.columns.index(header))

    def idx(self, header: str) -> int:
        return self.columns.index(header)

    @property
    def last_col(self) -> str:
        return col_letter(len(self.columns) - 1)

    def a1(self, cells: str) -> str:
        return f"'{self.name}'!{cells}"

    @property
    def data_range(self) -> str:
        return self.a1(f"A2:{self.last_col}{self.max_row}")

    @property
    def id_range(self) -> str:
        return self.a1(f"A2:A{self.max_row}")


TASKS = TabDef("Tasks", ("TaskID", "Title", "Area", "GoalID", "Priority", "Status", "DueDate", "PlannedDate",
                         "EstMinutes", "Flexibility", "Notes", "CreatedBy", "CreatedAt", "UpdatedAt",
                         "CompletedDate"), "T", 4, 1000)
SCHEDULE = TabDef("Schedule", ("EventID", "Date", "Start", "End", "Title", "Type", "Area", "LinkedID",
                               "Flexibility", "Status", "Location", "Notes", "CalendarEventID", "CreatedBy",
                               "UpdatedAt"), "E", 4, 3000)
GOALS = TabDef("Goals", ("GoalID", "Title", "Level", "Area", "ParentGoalID", "StartDate", "EndDate", "Metric",
                         "Target", "Current", "Status", "Why / Notes", "CreatedBy", "UpdatedAt"), "G", 3, 301)
ROUTINES = TabDef("Routines", ("RoutineID", "Title", "Area", "Days", "WindowStart", "WindowEnd", "DurationMin",
                               "Flexibility", "Type", "Active", "Notes", "UpdatedAt"), "R", 2, 100)
HABITS = TabDef("Habits", ("Date", "Day", "H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "Notes"), None, 0, 427)
INBOX = TabDef("Inbox", ("InboxID", "CapturedAt", "Text", "Status", "ConvertedToID", "Notes", "CreatedBy"),
               "I", 4, 1000)
REVIEWS = TabDef("Reviews", ("ReviewID", "Type", "PeriodStart", "Wins", "Challenges", "Lessons", "NextFocus",
                             "Energy1to5", "Mood1to5", "CreatedBy", "CreatedAt"), "RV", 4, 1000)
CHANGELOG = TabDef("ChangeLog", ("Timestamp", "Actor", "Action", "Tab", "ItemID", "Field", "OldValue", "NewValue",
                                 "Reason", "RequestID"), None, 0, 5000)
GOALCALC = TabDef("_GoalCalc", ("GoalID", "TasksOpen", "TasksDone", "MetricPct", "Children", "Own", "L2", "L3",
                                "Progress"), None, 0, 301)

DATA_TABS = {t.name: t for t in (TASKS, SCHEDULE, GOALS, ROUTINES, HABITS, INBOX, REVIEWS, CHANGELOG)}
# Tabs the bot must never write to.
READ_ONLY_TABS = frozenset({"Settings", "Dashboard", "Daily", "Weekly", "Monthly", "Yearly", "README_AI",
                            "_GoalCalc"})

HABITS_FIRST_DATE = (2026, 11, 1)   # Habits!A2
SETTINGS_KEYS_RANGE = "'Settings'!A2:B13"
SETTINGS_LISTS_RANGE = "'Settings'!E1:R12"
HABITS_HEADER_RANGE = "'Habits'!A1:K1"
