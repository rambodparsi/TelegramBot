"""In-memory Google Sheet with the Sheets REST semantics the planner relies on.

Loaded from tests/fixtures/Life_Planner.xlsx (the owner's template with sample data).
Dates/times are stored the way UNFORMATTED_VALUE + SERIAL_NUMBER returns them.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from http_client import HttpResponse

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "Life_Planner.xlsx"
EPOCH = dt.datetime(1899, 12, 30)
PROTECTED = {"Settings", "Dashboard", "Daily", "Weekly", "Monthly", "Yearly", "README_AI", "_GoalCalc"}


def _serial(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        delta = value - EPOCH
        return delta.days + delta.seconds / 86400 if delta.seconds else delta.days
    if isinstance(value, dt.date):
        return (value - EPOCH.date()).days
    if isinstance(value, dt.timedelta):
        return value.total_seconds() / 86400
    if isinstance(value, dt.time):
        return (value.hour * 60 + value.minute) / 1440
    return value


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def parse_a1(a1: str) -> tuple[str, int, int, int, int]:
    """'Tab'!A2:O1000 -> (tab, row0, col0, row1, col1), 1-based rows, 0-based cols, inclusive."""
    tab, cells = a1.rsplit("!", 1)
    tab = tab[1:-1].replace("''", "'") if tab.startswith("'") else tab
    parts = cells.split(":")
    m0 = re.fullmatch(r"([A-Z]+)(\d+)", parts[0])
    m1 = re.fullmatch(r"([A-Z]+)(\d+)", parts[-1])
    return tab, int(m0.group(2)), _col_index(m0.group(1)), int(m1.group(2)), _col_index(m1.group(1))


class FakeSheets:
    def __init__(self, tabs: dict[str, list[list[Any]]]):
        self.tabs = tabs
        self.get_calls = 0
        self.update_calls = 0
        self.updates: list[list[dict]] = []
        self._fail: list[int] = []

    @classmethod
    def from_xlsx(cls, path: Path = FIXTURE, request_id_header: bool = True) -> FakeSheets:
        import openpyxl

        wb = openpyxl.load_workbook(path)
        tabs: dict[str, list[list[Any]]] = {}
        for ws in wb.worksheets:
            grid = []
            for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=ws.max_column):
                vals = []
                for c in row:
                    v = c.value
                    vals.append(None if isinstance(v, str) and v.startswith("=") else _serial(v))
                grid.append(vals)
            tabs[ws.title] = grid
        # Computed cells the fake can't evaluate: habit names in Habits!C1:J1 come from Settings!N2:N9.
        settings = tabs["Settings"]
        for i in range(8):
            name = settings[1 + i][13] if len(settings) > 1 + i and len(settings[1 + i]) > 13 else None
            tabs["Habits"][0][2 + i] = name or "(unused)"
        goals = tabs["Goals"]
        tabs["_GoalCalc"] = [["GoalID", "", "", "", "", "", "", "", "Progress"]] + [
            [g[0] if g and g[0] else None] + [None] * 8 for g in goals[1:]]
        if request_id_header:
            cls._set(tabs, "ChangeLog", 1, 9, "RequestID")
        return cls(tabs)

    # --- grid helpers -------------------------------------------------------------------
    @staticmethod
    def _set(tabs: dict, tab: str, row: int, col: int, value: Any) -> None:
        grid = tabs.setdefault(tab, [])
        while len(grid) < row:
            grid.append([])
        line = grid[row - 1]
        while len(line) <= col:
            line.append(None)
        line[col] = value

    def cell(self, tab: str, a1: str) -> Any:
        m = re.fullmatch(r"([A-Z]+)(\d+)", a1)
        row, col = int(m.group(2)), _col_index(m.group(1))
        grid = self.tabs.get(tab, [])
        return grid[row - 1][col] if row - 1 < len(grid) and col < len(grid[row - 1]) else None

    def rows(self, tab: str) -> list[list[Any]]:
        return self.tabs.get(tab, [])

    def fail_next(self, status: int) -> None:
        self._fail.append(status)

    # --- REST ---------------------------------------------------------------------------
    def _read(self, a1: str) -> list[list[Any]]:
        tab, r0, c0, r1, c1 = parse_a1(a1)
        grid = self.tabs.get(tab, [])
        out = []
        for r in range(r0, min(r1, len(grid)) + 1):
            line = grid[r - 1][c0:c1 + 1]
            while line and line[-1] in (None, ""):
                line = line[:-1]
            out.append(line)
        while out and not out[-1]:
            out.pop()
        return out

    @staticmethod
    def _user_entered(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value == "":
            return None  # Google stores an empty write as an empty cell
        if value.startswith("'"):
            return value[1:]
        if value in ("TRUE", "FALSE"):
            return value == "TRUE"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return _serial(dt.date.fromisoformat(value))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value):
            return _serial(dt.datetime.strptime(value, "%Y-%m-%d %H:%M"))
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if m:
            return (int(m.group(1)) * 60 + int(m.group(2))) / 1440
        if re.fullmatch(r"-?\d+(\.\d+)?", value):
            return float(value) if "." in value else int(value)
        return value

    def handle(self, method: str, url: str, body: Any) -> HttpResponse:
        if self._fail:
            status = self._fail.pop(0)
            return HttpResponse(status, json.dumps({"error": {"message": "simulated"}}))
        parsed = urlparse(url)
        if method == "GET" and parsed.path.endswith("/values:batchGet"):
            self.get_calls += 1
            ranges = [unquote(r) for r in parse_qs(parsed.query)["ranges"]]
            return HttpResponse(200, json.dumps({"valueRanges": [
                {"range": r, "values": self._read(r)} for r in ranges]}))
        if method == "POST" and parsed.path.endswith("/values:batchUpdate"):
            payload = json.loads(body)
            for item in payload["data"]:
                tab = parse_a1(item["range"])[0]
                assert tab not in PROTECTED, f"write to protected tab {tab}"
            self.update_calls += 1
            self.updates.append(payload["data"])
            for item in payload["data"]:
                tab, r0, c0, _, _ = parse_a1(item["range"])
                for dr, line in enumerate(item["values"]):
                    for dc, value in enumerate(line):
                        self._set(self.tabs, tab, r0 + dr, c0 + dc, self._user_entered(value))
            return HttpResponse(200, "{}")
        raise AssertionError(f"unexpected Sheets call {method} {url}")


class FakeSigner:
    async def sign_rs256(self, data: bytes) -> bytes:
        return b"sig"


SERVICE_ACCOUNT_JSON = json.dumps({"client_email": "bot@test.iam.gserviceaccount.com",
                                   "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
                                   "token_uri": "https://oauth2.googleapis.com/token"})
