"""Planner Phase 2: safe writes, ChangeLog, retry safety, and the end-to-end message flow."""

import datetime as dt

import pytest

import app
from http_client import HttpResponse
from planner.schema import CHANGELOG, HABITS, INBOX, SCHEDULE, TASKS
from test_planner_flow import NOW, _planner_svc
from test_app import _update
from test_tools import Script, _tool_results, call, text

TODAY = NOW.date()  # Tue 2026-11-24


def _svc():
    db, http, svc = _planner_svc(Script())
    planner = svc.planner_for("America/Chicago")
    return db, http.sheets, svc, planner


def _row(sheet, tab, item_id):
    for i, r in enumerate(sheet.rows(tab)):
        if r and r[0] == item_id:
            return i + 1, r
    return None, None


def _log(sheet):
    return [r for r in sheet.rows("ChangeLog")[1:] if r and r[0] is not None]


async def test_add_task_appends_row_logs_and_is_one_write():
    db, sheet, svc, p = _svc()
    res = await p.run_op("k1", lambda: p.add_task("k1", "Call the bank", "Money", "P1",
                                                   planned_date=dt.date(2026, 11, 26)))
    assert res["ok"] and res["changed"] == ["T-0012"] and sheet.update_calls == 1
    row_no, row = _row(sheet, "Tasks", "T-0012")
    assert row_no == 13 and row[1] == "Call the bank" and row[2] == "Money" and row[5] == "Not started"
    assert row[9] == "Flexible" and row[11] == "ai" and row[7] == 46352  # 2026-11-26 as a serial date
    log = _log(sheet)[-1]
    assert log[1:6] == ["ai", "create", "Tasks", "T-0012", None] and log[7] == "Call the bank" and log[9] == "k1"


async def test_add_task_dedupes_and_sanitizes_formula():
    _, sheet, _, p = _svc()
    res = await p.add_task("k", "Prepare slides for team meeting", "Work", planned_date=TODAY)
    assert res["changed"] == [] and "Already in your list" in res["summary"] and sheet.update_calls == 0
    await p.add_task("k2", "=IMPORTXML(1)", "Work")
    _, row = _row(sheet, "Tasks", "T-0012")
    assert sheet.updates[-1][0]["values"][0][1] == "'=IMPORTXML(1)" and row[1] == "=IMPORTXML(1)"


async def test_schedule_overlap_returns_suggestions_without_writing():
    _, sheet, _, p = _svc()
    res = await p.run_op("k", lambda: p.schedule("k", TODAY, 9 * 60 + 30, 10 * 60 + 30, title="Gym", area="Health"))
    assert res["error"] == "overlap" and res["conflict"]["id"] == "E-0013"
    assert res["suggestions"] and sheet.update_calls == 0


async def test_schedule_task_block_links_task_and_sets_planned_date():
    _, sheet, _, p = _svc()
    res = await p.schedule("k", dt.date(2026, 11, 30), 15 * 60, None, 60, task_id="T-0008")
    assert res["ok"], res
    _, ev = _row(sheet, "Schedule", res["data"]["event_id"])
    assert ev[5] == "Task block" and ev[7] == "T-0008" and ev[8] == "Flexible" and ev[2] == 0.625
    _, task = _row(sheet, "Tasks", "T-0008")
    assert task[7] == 46356 and sheet.update_calls == 1


async def test_outside_hours_and_midnight_rules():
    _, _, _, p = _svc()
    with pytest.raises(Exception) as err:
        await p.schedule("k", TODAY, 5 * 60, 5 * 60 + 30, title="Early", area="Health")
    assert err.value.code == "outside_hours"
    with pytest.raises(Exception) as err:
        await p.schedule("k", TODAY, 23 * 60 + 30, 30, title="x", area="Fun")
    assert err.value.code == "invalid_argument"


async def test_move_flexible_writes_and_logs_each_field():
    _, sheet, _, p = _svc()
    res = await p.move("mv", "E-0018", start=21 * 60 + 45)
    assert res["ok"] and "22:15" in res["summary"]
    log = [r for r in _log(sheet) if len(r) > 9 and r[9] == "mv"]
    assert [(r[2], r[5], r[6], r[7]) for r in log] == [("move", "Start", "21:00", "21:45"),
                                                     ("move", "End", "21:30", "22:15")]


async def test_fixed_items_need_confirmation_and_are_not_written():
    _, sheet, _, p = _svc()
    res = await p.run_op("fx", lambda: p.move("fx", "E-0017", start=19 * 60 + 30))
    assert res["needs_confirmation"] and "FIXED" in res["summary"] and sheet.update_calls == 0
    assert res["_pending"]["op"] == "move" and res["_pending"]["args"]["start"] == "19:30"
    res2 = await p.cancel("c", "E-0017")
    assert res2["needs_confirmation"]
    done = await p.move("fx2", "E-0017", start=19 * 60 + 30, confirmed=True)
    assert done["ok"] and sheet.update_calls == 1


async def test_focus_cap_asks_for_confirmation():
    _, sheet, _, p = _svc()
    res = await p.schedule("k", TODAY, 15 * 60, None, 150, title="More deep work", area="Work", type="Focus")
    assert res["needs_confirmation"] and "limit 4 h" in res["summary"] and sheet.update_calls == 0


async def test_complete_task_marks_today_blocks_done():
    _, sheet, _, p = _svc()
    res = await p.complete_task("done", "T-0007")
    assert res["ok"] and "1 time block" in res["summary"]
    assert _row(sheet, "Tasks", "T-0007")[1][5] == "Done" and _row(sheet, "Tasks", "T-0007")[1][14] == 46350
    assert _row(sheet, "Schedule", "E-0015")[1][9] == "Done"
    again = await p.complete_task("done2", "T-0007")
    assert "already done" in again["summary"]


async def test_cancel_task_cancels_its_future_blocks():
    _, sheet, _, p = _svc()
    res = await p.cancel("c", "T-0001", "Report moved to next quarter")
    assert res["ok"]
    assert _row(sheet, "Tasks", "T-0001")[1][5] == "Cancelled"
    assert _row(sheet, "Schedule", "E-0022")[1][9] == "Cancelled"  # Wed block
    assert _row(sheet, "Schedule", "E-0003")[1][9] == "Done"         # past block untouched
    assert _log(sheet)[-1][8] == "Report moved to next quarter"


async def test_log_habit_capture_and_goal():
    _, sheet, _, p = _svc()
    res = await p.log_habit("h", "exercise")
    assert res["ok"] and sheet.cell("Habits", "C25") is True  # row 25 = 2026-11-24
    with pytest.raises(Exception) as err:
        await p.log_habit("h2", "Swim")
    assert "Exercise" in err.value.message
    cap = await p.capture("i", "Sometime learn to sail")
    assert cap["changed"] == ["I-0005"] and _row(sheet, "Inbox", "I-0005")[1][3] == "New"
    goal = await p.update_goal("g", "G-002", current=2000)
    assert goal["ok"] and _row(sheet, "Goals", "G-002")[1][9] == 2000 and "2000 / 10000" in goal["summary"]


async def test_sheet_not_ready_without_request_id_header():
    _, sheet, _, p = _svc()
    sheet.tabs["ChangeLog"][0][9] = None
    res = await p.run_op("k", lambda: p.capture("k", "x"))
    assert res["error"] == "sheet_not_ready" and sheet.update_calls == 0


async def test_updates_find_rows_by_id_after_owner_reorders_tab():
    _, sheet, _, p = _svc()
    await p.store.load(TASKS)  # cached before the human edit
    grid = sheet.tabs["Tasks"]
    grid[1], grid[2] = grid[2], grid[1]  # owner swaps T-0001 and T-0002 rows
    await p.complete_task("k", "T-0002")
    assert grid[1][0] == "T-0002" and grid[1][5] == "Done" and grid[2][5] == "In progress"


async def test_idempotency_replay_and_crash_after_commit():
    db, sheet, svc, p = _svc()
    fn = lambda: p.capture("same", "idea")  # noqa: E731
    first = await p.run_op("same", fn)
    second = await p.run_op("same", fn)
    assert second["replayed"] and second["changed"] == first["changed"] and sheet.update_calls == 1
    # crash after the sheet write but before finish_op
    p2 = svc.planner_for("America/Chicago")
    await svc.repo.start_op("crash")
    await p2.capture("crash", "landed")
    again = await p2.run_op("crash", lambda: p2.capture("crash", "landed"))
    assert again["summary"] == "Already done." and sheet.update_calls == 2
    # validation errors are replayed too
    bad = await p.run_op("bad", lambda: p.move("bad", "E-9999", start=600))
    assert bad["error"] == "not_found"
    assert (await p.run_op("bad", lambda: p.move("bad", "E-9999", start=600)))["replayed"]


async def test_message_flow_retry_after_gemini_quota_does_not_duplicate():
    busy = HttpResponse(429, '{"error":{"message":"quota"}}')
    script = Script(call("planner_add_task", title="Call the bank", area="Money", priority="P1"), busy, busy, busy,
                    call("planner_add_task", title="Call the bank", area="Money", priority="P1"),
                    text("✅ Added 'Call the bank'"))
    db, http, svc = _planner_svc(script)
    with pytest.raises(app.RetryLater):
        await app.handle_update(_update(1, "add call the bank"), svc, attempt=1, max_attempts=4)
    assert http.sheets.update_calls == 1
    assert await app.handle_update(_update(1, "add call the bank"), svc, attempt=2, max_attempts=4) == "answered:text"
    assert http.sheets.update_calls == 1  # second attempt replayed the stored result
    assert len([r for r in http.sheets.rows("Tasks") if r and r[1] == "Call the bank"]) == 1
    replay = _tool_results(script.requests[-1])[0]["response"]
    assert replay["replayed"] and replay["changed"] == ["T-0012"]
    thread = db.rows("SELECT thread_id FROM conversations")[0][0]
    state = await svc.graph.aget_state({"configurable": {"thread_id": thread}})
    assert [m.type for m in state.values["messages"]].count("human") == 1  # retry didn't duplicate the turn


async def test_sheets_outage_during_write_retries_message():
    script = Script(call("planner_capture", text="idea"), call("planner_capture", text="idea"), text("ok"))
    _, http, svc = _planner_svc(script)
    http.sheets.fail_next(503)
    with pytest.raises(app.RetryLater):
        await app.handle_update(_update(1, "note: idea"), svc, attempt=1, max_attempts=4)
    assert await app.handle_update(_update(1, "note: idea"), svc, attempt=2, max_attempts=4) == "answered:text"
    assert len([r for r in http.sheets.rows("Inbox") if r and r[2] == "idea"]) == 1
