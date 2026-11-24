"""Planner Phase 1: values, rules, settings parsing, store, auth, Sheets client."""

import datetime as dt

import pytest

from fakes import FakeHttp
from planner import rules
from planner import values as v
from planner.google_auth import ServiceAccountAuth
from planner.model import Event, Settings
from planner.schema import SCHEDULE, TASKS, col_letter
from planner.sheets import SheetsClient, SheetsError
from planner.store import PlannerStore
from sheets_fake import SERVICE_ACCOUNT_JSON, FakeSheets, FakeSigner
from tools.registry import ToolInputError, validate


# --- values ------------------------------------------------------------------------------
def test_serial_dates_and_times():
    assert v.to_date(46350) == dt.date(2026, 11, 24)
    assert v.to_date("2026-11-24") == dt.date(2026, 11, 24)
    assert v.to_minutes(0.25) == 360 and v.to_minutes(1.0) == 1440 and v.to_minutes(0) == 0
    assert v.to_minutes("07:30") == 450
    assert v.fmt_minutes(1440) == "24:00"
    assert v.to_datetime(46350.5) == dt.datetime(2026, 11, 24, 12, 0)


def test_digits_and_sanitize():
    assert v.normalize_digits("۲۰۲۶-۱۱-۲۴ ٠٨:٣٠") == "2026-11-24 08:30"
    assert v.sanitize_text("=HYPERLINK(1)", 50) == "'=HYPERLINK(1)"
    assert v.sanitize_text("- buy milk", 50) == "'- buy milk"
    assert v.sanitize_text("2026-01-01", 50) == "'2026-01-01"
    assert v.sanitize_text("1/2", 50) == "'1/2" and v.sanitize_text("42", 50) == "'42"
    assert v.sanitize_text("Call  the\nbank", 50) == "Call the bank"
    assert v.sanitize_text("x" * 200, 120) == "x" * 120
    assert v.sanitize_text("ورزش صبح", 50) == "ورزش صبح"


# --- rules -------------------------------------------------------------------------------
def _ev(i, day, start, end, status="Planned", type_="Event"):
    return Event(i, f"E-{i:04d}", day, start, end, "x", type_, "Work", "", "Flexible", status, "", "", "me")


D = dt.date(2026, 11, 24)  # a Tuesday


def test_overlap_edges_and_cancelled_ignored():
    evs = [_ev(1, D, 540, 600), _ev(2, D, 600, 660, status="Cancelled")]
    assert not rules.overlaps(540, 600, 600, 660)  # touching
    assert [e.id for e in rules.conflicts(evs, D, 570, 630)] == ["E-0001"]
    assert rules.conflicts(evs, D, 600, 700) == []


def test_free_slots_buffer_workhours_and_today():
    s = Settings()
    evs = [_ev(1, D, 540, 660)]  # 09:00-11:00 busy
    work = rules.find_free_slots(evs, D, 60, "Work", s, dt.datetime(2026, 11, 23, 8, 0))
    assert work[0] == (675, 735)  # 11:15 (after 10-min buffer, rounded to 15)
    personal = rules.find_free_slots(evs, D, 60, "Health", s, dt.datetime(2026, 11, 23, 8, 0))
    assert personal[0][0] < 540 or personal[0][0] >= 1020  # outside work hours first
    today = rules.find_free_slots([], D, 30, None, s, dt.datetime(2026, 11, 24, 13, 7))
    assert today[0][0] >= 13 * 60 + 15
    assert rules.find_free_slots([], D, 30, None, s, dt.datetime(2026, 11, 25, 8, 0)) == []  # past day
    sat = rules.find_free_slots([], dt.date(2026, 11, 28), 60, None, s, dt.datetime(2026, 11, 23, 8, 0))
    assert sat[0][0] >= 9 * 60


def test_focus_routines_ids():
    evs = [_ev(1, D, 540, 660, type_="Focus"), _ev(2, D, 700, 760, type_="Task block"), _ev(3, D, 800, 900)]
    assert rules.focus_minutes(evs, D) == 180
    assert rules.routine_runs_on("Mon,Wed,Fri", 2) is True and rules.routine_runs_on("Mon, wed", 1) is False
    assert rules.routine_runs_on("Weekdays", 5) is False and rules.routine_runs_on("Funday", 0) is None
    assert rules.next_id("T", ["T-0001", "T-0012", "X-9", ""], 4) == "T-0013"
    assert rules.next_id("G", [], 3) == "G-001" and rules.next_id("RV", ["RV-0001"], 4) == "RV-0002"


# --- settings + store (real workbook) ----------------------------------------------------
@pytest.fixture
def sheets():
    fake = FakeSheets.from_xlsx()
    http = FakeHttp()
    http.sheets = fake
    client = SheetsClient("sheet123", ServiceAccountAuth(SERVICE_ACCOUNT_JSON, http, FakeSigner()), http)
    return fake, http, client


async def test_settings_lists_ignore_notes_below_lists(sheets):
    _, _, client = sheets
    store = PlannerStore(client)
    s = await store.settings()
    assert s.lists["Areas"] == ["Work", "Health", "Money", "Family", "Personal growth", "Home", "Fun"]
    assert s.lists["Habits"] == ["Exercise", "Read 20 min", "Drink 2L water", "Meditate 10 min", "Journal",
                                 "No phone after 23:00"]
    assert (s.day_start, s.day_end, s.work_hours, s.buffer_minutes) == (360, 1440, (540, 1020), 10)
    assert s.quiet_hours == (1380, 360) and s.max_focus_minutes == 240 and s.planning_year == 2027
    assert await store.habit_names() == ["Exercise", "Read 20 min", "Drink 2L water", "Meditate 10 min",
                                         "Journal", "No phone after 23:00", "", ""]


def test_settings_malformed_falls_back():
    s = Settings.parse([["WorkHours", "nine to five"], ["BufferMinutes", "x"]], [])
    assert s.work_hours == (540, 1020) and s.buffer_minutes == 10 and s.lists["Areas"][0] == "Work"


async def test_store_parses_tabs_in_one_read_and_caches(sheets):
    fake, _, client = sheets
    store = PlannerStore(client)
    await store.load_many([SCHEDULE, TASKS])
    events, tasks = await store.load(SCHEDULE), await store.load(TASKS)
    assert fake.get_calls == 1  # settings piggy-backed on the same batchGet
    first = events[0]
    assert (first.id, first.date, first.start, first.end, first.row) == ("E-0001", dt.date(2026, 11, 23), 360, 420, 2)
    assert tasks[0].id == "T-0001" and tasks[0].due == dt.date(2026, 11, 25) and tasks[0].est_minutes == 120
    await store.load(TASKS)
    assert fake.get_calls == 1
    habits = await store.habits_for(dt.date(2026, 11, 1), dt.date(2026, 11, 2))
    assert habits[dt.date(2026, 11, 1)]["Exercise"] is True and habits[dt.date(2026, 11, 2)]["Exercise"] is False


# --- auth + client ---------------------------------------------------------------------------
async def test_token_cached_and_refreshed_before_expiry():
    from planner import google_auth

    google_auth._TOKENS.clear()
    http, clock = FakeHttp(), [1000.0]
    auth = ServiceAccountAuth(SERVICE_ACCOUNT_JSON, http, FakeSigner(), clock=lambda: clock[0])
    assert await auth.token() == "test-token"
    await auth.token()
    assert http.token_calls == 1
    clock[0] += 3599 - 30  # within 60 s of expiry
    await auth.token()
    assert http.token_calls == 2
    jwt_body = [b for u, b in http.calls if "oauth2" in u][0]
    assert jwt_body.startswith("grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=")
    assert jwt_body.count(".") == 2


async def test_sheets_401_refreshes_once_and_5xx_is_retryable(sheets):
    from planner import google_auth

    fake, http, client = sheets
    google_auth._TOKENS.clear()
    fake.fail_next(401)
    await client.batch_get(["'Tasks'!A1:B1"])
    assert http.token_calls == 2
    fake.fail_next(503)
    with pytest.raises(SheetsError) as err:
        await client.batch_get(["'Tasks'!A1:B1"])
    assert err.value.retryable
    with pytest.raises(SheetsError):
        await client.batch_update([("'Daily'!A1", [["x"]])])  # view tabs are never written


# --- validator ---------------------------------------------------------------------------------
def test_validator_formats():
    schema = {"type": "object", "properties": {
        "d": {"type": "string", "format": "date"}, "t": {"type": "string", "format": "time"},
        "n": {"type": "integer", "minimum": 5, "maximum": 720}, "b": {"type": "boolean"},
        "x": {"type": "number"}, "id": {"type": "string", "pattern": r"^T-\d{4}$"},
        "c": {"type": "string", "format": "date", "allowNone": True}}}
    out = validate(schema, {"d": "۲۰۲۶-۱۱-۲۴", "t": "8:05", "n": "۴۵", "b": "true", "x": "1,500.5",
                            "id": "T-0001", "c": "none"})
    assert out == {"d": "2026-11-24", "t": "08:05", "n": 45, "b": True, "x": 1500.5, "id": "T-0001", "c": "none"}
    for bad in ({"d": "Tuesday"}, {"d": "2026-02-30"}, {"t": "25:00"}, {"t": "24:30"}, {"n": 3},
                {"n": 1.5}, {"b": "yes"}, {"id": "T-1"}):
        with pytest.raises(ToolInputError):
            validate(schema, bad)
    assert validate(schema, {"t": "24:00"}) == {"t": "24:00"}


def test_col_letters():
    assert [col_letter(i) for i in (0, 14, 25, 26)] == ["A", "O", "Z", "AA"]
