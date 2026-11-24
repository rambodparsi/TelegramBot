"""Planner read tools end to end: Telegram update -> graph -> Gemini (scripted) -> Sheets (fake)."""

import datetime as dt
import json

import pytest

import app
from fakes import FakeD1, FakeHttp
from planner import google_auth
from sheets_fake import SERVICE_ACCOUNT_JSON, FakeSheets, FakeSigner
from test_app import OWNER, _cfg, _update
from test_tools import Script, _tool_results, call, text

NOW = dt.datetime(2026, 11, 24, 8, 0)  # Tuesday morning, owner's local time


def _planner_svc(script, **cfg_over):
    google_auth._TOKENS.clear()
    db, http = FakeD1(), FakeHttp(script)
    http.sheets = FakeSheets.from_xlsx()
    cfg = _cfg(**{"planner_sheet_id": "sheet123", "google_service_account_json": SERVICE_ACCOUNT_JSON, **cfg_over})
    svc = app.Services(cfg, db, http)
    if svc.sheets:
        svc.sheets._auth._signer = FakeSigner()
    svc.clock = lambda: NOW
    return db, http, svc


def _declared(body):
    return {d["name"] for t in body.get("tools", []) for d in t["functionDeclarations"]}


async def test_whats_on_today_reads_sheet_once_and_answers():
    script = Script(call("planner_get_agenda", start_date="2026-11-24"), text("Here's your Tuesday…"))
    _, http, svc = _planner_svc(script)
    assert await app.handle_update(_update(1, "What's on today?"), svc) == "answered:text"
    result = _tool_results(script.requests[1])[0]["response"]
    day = result["data"]["days"][0]
    assert result["ok"] and day["date"] == "2026-11-24" and day["weekday"] == "Tuesday"
    assert day["events"] and all(e["start"] <= day["events"][i + 1]["start"]
                                 for i, e in enumerate(day["events"][:-1]))
    assert {"id", "start", "end", "title", "type", "flexibility"} <= set(day["events"][0])
    assert day["tasks"][-1]["status"] == "Done" or all(t["status"] != "Done" for t in day["tasks"])
    assert "Exercise" in day.get("habits", {})
    assert http.sheets.get_calls <= 2 and http.sheets.update_calls == 0
    prompt = script.requests[0]["systemInstruction"]["parts"][0]["text"]
    assert "Life Planner" in prompt
    assert {"planner_get_agenda", "planner_find_free_slots", "planner_goals"} <= _declared(script.requests[0])


async def test_week_agenda_and_range_limit():
    script = Script(call("planner_get_agenda", start_date="2026-11-23", end_date="2026-11-29"), text("ok"),
                    call("planner_get_agenda", start_date="2026-11-01", end_date="2026-11-30"), text("ok"))
    _, _, svc = _planner_svc(script)
    await app.handle_update(_update(1, "my week?"), svc)
    week = _tool_results(script.requests[1])[0]["response"]
    assert len(week["data"]["days"]) == 7 and "overdue" in week["data"]
    await app.handle_update(_update(2, "whole month?"), svc)
    err = _tool_results(script.requests[3])[-1]["response"]
    assert err == {"ok": False, "error": "invalid_argument", "message": "agenda range is limited to 14 days"}


async def test_free_slots_and_goals():
    script = Script(call("planner_find_free_slots", date="2026-11-25", minutes=60, area="Work"),
                    call("planner_goals", level="Year"), text("ok"))
    _, _, svc = _planner_svc(script)
    await app.handle_update(_update(1, "when can I fit an hour tomorrow, and how are my goals?"), svc)
    slots, goals = [r["response"] for r in _tool_results(script.requests[2])]
    assert slots["ok"] and slots["data"]["slots"] and all("start" in s for s in slots["data"]["slots"])
    assert goals["ok"] and {g["level"] for g in goals["data"]["goals"]} == {"Year"}
    assert all(g["id"].startswith("G-") for g in goals["data"]["goals"])


async def test_bad_area_and_bad_date_are_reported_not_raised():
    script = Script(call("planner_find_free_slots", date="2026-11-25", minutes=60, area="Gym"),
                    call("planner_get_agenda", start_date="tomorrow"), text("ok"))
    _, _, svc = _planner_svc(script)
    await app.handle_update(_update(1, "x"), svc)
    area_err, date_err = [r["response"] for r in _tool_results(script.requests[2])]
    assert area_err["error"] == "invalid_argument" and "Work" in area_err["message"]
    assert "YYYY-MM-DD" in date_err["error"]


async def test_sheet_access_denied_gives_actionable_message():
    script = Script(call("planner_get_agenda", start_date="2026-11-24"), text("ok"))
    _, http, svc = _planner_svc(script)
    http.sheets.fail_next(403)
    await app.handle_update(_update(1, "today?"), svc)
    res = _tool_results(script.requests[1])[0]["response"]
    assert res["error"] == "sheet_unavailable" and "shared with the service account" in res["message"]


async def test_planner_disabled_hides_tools_and_other_users_dont_get_it():
    script = Script(text("ok"))
    _, _, svc = _planner_svc(script, planner_sheet_id=None)
    await app.handle_update(_update(1, "today?"), svc)
    assert not any(n.startswith("planner_") for n in _declared(script.requests[0]))
    assert "Life Planner" not in script.requests[0]["systemInstruction"]["parts"][0]["text"]

    script2 = Script(text("ok"))
    _, _, svc2 = _planner_svc(script2, authorized_user_ids=frozenset({OWNER, 1_000_000_000}))
    await app.handle_update(_update(5, "today?", user=1_000_000_000), svc2)
    assert not any(n.startswith("planner_") for n in _declared(script2.requests[0]))
