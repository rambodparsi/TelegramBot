"""Planner Phase 4: hourly tick -> morning brief / evening check-in, once per day, owner's local hour."""

import datetime as dt

import app
from test_app import OWNER
from test_planner_flow import _planner_svc
from test_tools import Script

UTC = dt.timezone.utc
BRIEF_UTC = dt.datetime(2026, 11, 24, 13, 5, tzinfo=UTC)    # 07:05 in Chicago (CST, UTC-6)
CHECKIN_UTC = dt.datetime(2026, 11, 25, 3, 5, tzinfo=UTC)   # 21:05 on Nov 24 in Chicago


def _svc(**cfg):
    return _planner_svc(Script(), **{"planner_brief_hour": 7, "planner_checkin_hour": 21, **cfg})


async def test_morning_brief_sent_once_at_local_hour():
    db, http, svc = _svc()
    assert await app.planner_tick(svc, BRIEF_UTC) == "planner:brief:sent"
    msg = http.telegram("sendMessage")[-1]
    assert msg["chat_id"] == OWNER and msg["parse_mode"] == "HTML"
    text = msg["text"]
    assert text.startswith("☀️ <b>Tuesday, Nov 24</b>")
    assert "09:00–11:00 Deep work: Q4 report draft" in text
    assert "<b>To-do</b>" in text and "Prepare slides for team meeting (P1)" in text or "(P2)" in text
    assert "⚠️ Overdue: Book dentist appointment" in text
    assert "<b>Habits</b>: Exercise" in text
    assert await app.planner_tick(svc, BRIEF_UTC + dt.timedelta(minutes=20)) == "planner:brief:already_sent"
    assert len(http.telegram("sendMessage")) == 1
    assert await app.planner_tick(svc, BRIEF_UTC + dt.timedelta(hours=1)) == "planner:idle"
    assert http.sheets.update_calls == 0  # read-only


async def test_brief_goes_into_the_conversation_for_follow_ups():
    db, http, svc = _svc()
    await app.planner_tick(svc, BRIEF_UTC)
    thread = db.rows("SELECT thread_id FROM conversations")[0][0]
    state = await svc.graph.aget_state({"configurable": {"thread_id": thread}})
    last = state.values["messages"][-1]
    assert last.type == "ai" and last.content.startswith("☀️ Tuesday, Nov 24") and "<b>" not in last.content
    assert db.rows("SELECT role FROM messages") == [("assistant",)]


async def test_evening_checkin_lists_open_tasks_and_unlogged_habits():
    _, http, svc = _svc()
    assert await app.planner_tick(svc, CHECKIN_UTC) == "planner:checkin:sent"
    text = http.telegram("sendMessage")[-1]["text"]
    assert text.startswith("🌙 <b>Shutdown</b> — 0 of") and "Still open:" in text
    assert "Habits not logged yet:" in text


async def test_owner_timezone_preference_moves_the_hour():
    _, http, svc = _svc()
    await svc.repo.set_preference(OWNER, "timezone", "Asia/Tehran")  # UTC+3:30
    assert await app.planner_tick(svc, BRIEF_UTC) == "planner:idle"  # 16:35 in Tehran
    tehran_7am = dt.datetime(2026, 11, 24, 3, 35, tzinfo=UTC)
    assert await app.planner_tick(svc, tehran_7am) == "planner:brief:sent"


async def test_tick_is_off_without_planner_or_hours():
    _, _, svc = _svc(planner_sheet_id=None)
    assert await app.planner_tick(svc, BRIEF_UTC) == "planner:off"
    _, _, svc2 = _svc(planner_brief_hour=None, planner_checkin_hour=None)
    assert await app.planner_tick(svc2, BRIEF_UTC) == "planner:no_hours"


def test_brief_trims_to_eight_lines_dropping_routines_first():
    from planner.briefing import morning_brief

    ev = lambda h, t, typ: {"id": "E", "start": f"{h:02d}:00", "end": f"{h:02d}:30", "title": t,  # noqa: E731
                            "type": typ, "status": "Planned"}
    events = [ev(6 + i, f"Routine {i}", "Routine") for i in range(6)] + [ev(13 + i, f"Meeting {i}", "Event")
                                                                         for i in range(5)]
    agenda = {"data": {"days": [{"date": "2026-11-24", "events": events, "tasks": [], "habits": {}}],
                       "overdue": []}}
    text, plain = morning_brief(agenda)
    assert all(f"Meeting {i}" in text for i in range(5)) and "+3 more" in text and "<" not in plain
