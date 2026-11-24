"""Planner Phase 3: confirmation buttons, Undo button, undo semantics, button re-delivery on retry."""

import json

import pytest

import app
from test_app import OWNER, _update
from test_planner_flow import _planner_svc
from test_tools import Script, _tool_results, call, text


def _callback(uid, data, message_id=500):
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "data": data, "from": {"id": OWNER},
                                                  "message": {"message_id": message_id,
                                                              "chat": {"id": 42, "type": "private"}}}}


def _keyboard(http, index=-1):
    msg = http.telegram("sendMessage")[index]
    return {b["text"]: b["callback_data"] for row in msg.get("reply_markup", {}).get("inline_keyboard", [])
            for b in row}


def _row(sheet, tab, item_id):
    return next(r for r in sheet.rows(tab) if r and r[0] == item_id)


async def test_fixed_move_shows_buttons_and_confirm_writes_once():
    script = Script(call("planner_move", event_id="E-0017", start="19:30"),
                    text("Family dinner is Fixed. Confirm below?"))
    _, http, svc = _planner_svc(script)
    await app.handle_update(_update(1, "move family dinner to 7:30"), svc)
    assert _tool_results(script.requests[1])[0]["response"]["needs_confirmation"]
    keys = _keyboard(http)
    assert set(keys) == {"✅ Confirm", "✖️ Cancel"} and http.sheets.update_calls == 0
    assert await app.handle_update(_callback(2, keys["✅ Confirm"]), svc) == "callback:planner_confirm:ok"
    assert _row(http.sheets, "Schedule", "E-0017")[2] == pytest.approx(19.5 / 24) and http.sheets.update_calls == 1
    assert "Moved 'Family dinner'" in http.telegram("sendMessage")[-1]["text"]
    assert "↩️ Undo" in _keyboard(http)                      # the result message offers Undo
    assert http.telegram("editMessageReplyMarkup")            # buttons removed from the question
    assert await app.handle_update(_callback(3, keys["✅ Confirm"]), svc) == "callback:invalid"  # double tap
    assert http.sheets.update_calls == 1


async def test_cancel_button_changes_nothing():
    script = Script(call("planner_cancel", item_id="E-0017"), text("Needs approval."))
    _, http, svc = _planner_svc(script)
    await app.handle_update(_update(1, "cancel family dinner"), svc)
    keys = _keyboard(http)
    assert await app.handle_update(_callback(2, keys["✖️ Cancel"]), svc) == "callback:cancel:planner_confirm"
    assert http.sheets.update_calls == 0 and _row(http.sheets, "Schedule", "E-0017")[9] == "Planned"


async def test_undo_button_covers_all_writes_in_one_message():
    script = Script(call("planner_add_task", title="Call the bank", area="Money"),
                    call("planner_log_habit", habit="Exercise"),
                    text("✅ Added 'Call the bank'\n✅ Logged Exercise"))
    _, http, svc = _planner_svc(script)
    await app.handle_update(_update(1, "add call the bank, and I exercised"), svc)
    keys = _keyboard(http)
    assert list(keys) == ["↩️ Undo"]
    assert await app.handle_update(_callback(2, keys["↩️ Undo"]), svc) == "callback:planner_undo:ok"
    assert _row(http.sheets, "Tasks", "T-0012")[5] == "Cancelled"            # created task soft-removed
    assert http.sheets.cell("Habits", "C25") is None                          # habit restored to empty
    log = [r for r in http.sheets.rows("ChangeLog") if r and r[2] == "undo"]
    assert len(log) == 2 and all(str(r[8]).startswith("undo:u1:") for r in log)
    assert "2 changes reverted" in http.telegram("sendMessage")[-1]["text"]


async def test_undo_skips_fields_changed_since_and_reports_already_undone():
    script = Script(call("planner_move", event_id="E-0018", start="21:45"), text("Moved."),
                    call("planner_undo_last"), text("Undone."),
                    call("planner_undo_last"), text("Nothing."))
    _, http, svc = _planner_svc(script)
    await app.handle_update(_update(1, "move reading to 21:45"), svc)
    row = next(r for r in http.sheets.tabs["Schedule"] if r and r[0] == "E-0018")
    row[3] = 23 / 24  # the owner edits End by hand afterwards
    await app.handle_update(_update(2, "undo that"), svc)
    res = _tool_results(script.requests[3])[-1]["response"]
    assert res["ok"] and "changed since" in res["summary"]
    assert row[2] == pytest.approx(21 / 24) and row[3] == pytest.approx(23 / 24)  # Start reverted, End kept
    await app.handle_update(_update(3, "undo"), svc)
    again = _tool_results(script.requests[5])[-1]["response"]
    assert again["summary"] in ("Already undone.", "There's nothing recent to undo.")


async def test_undo_of_two_moves_restores_the_original_time():
    script = Script(call("planner_move", event_id="E-0018", start="21:45"),
                    call("planner_move", event_id="E-0018", start="22:00"), text("Moved twice."))
    _, http, svc = _planner_svc(script)
    await app.handle_update(_update(1, "move reading later, then later again"), svc)
    keys = _keyboard(http)
    await app.handle_update(_callback(2, keys["↩️ Undo"]), svc)
    row = _row(http.sheets, "Schedule", "E-0018")
    assert row[2] == pytest.approx(21 / 24) and row[3] == pytest.approx(21.5 / 24)


async def test_retried_delivery_resends_the_same_buttons():
    script = Script(call("planner_capture", text="learn to sail"), text("Saved."))
    db, http, svc = _planner_svc(script)
    real_send, fail = svc.telegram.send_message, {"n": 1}

    async def flaky(*a, **k):
        if fail["n"]:
            fail["n"] -= 1
            from telegram.client import TelegramError
            raise TelegramError("sendMessage", 502, "Bad Gateway")
        return await real_send(*a, **k)

    svc.telegram.send_message = flaky
    with pytest.raises(app.RetryLater):
        await app.handle_update(_update(1, "sometime learn to sail"), svc, attempt=1, max_attempts=4)
    stored = db.rows("SELECT response_buttons FROM telegram_updates")[0][0]
    assert await app.handle_update(_update(1, "sometime learn to sail"), svc, attempt=2, max_attempts=4) \
        == "answered:text"
    assert http.sheets.update_calls == 1
    assert [[b[0] for b in row] for row in json.loads(stored)] == [["↩️ Undo"]]
    assert list(_keyboard(http)) == ["↩️ Undo"]


async def test_focus_cap_confirmation_runs_schedule_when_confirmed():
    script = Script(call("planner_schedule", date="2026-11-24", start="15:00", minutes=150, title="More deep work",
                         area="Work", type="Focus"), text("That's over your focus limit — confirm?"))
    _, http, svc = _planner_svc(script)
    await app.handle_update(_update(1, "add 2.5h deep work today at 3"), svc)
    keys = _keyboard(http)
    assert await app.handle_update(_callback(2, keys["✅ Confirm"]), svc) == "callback:planner_confirm:ok"
    assert any(r and r[4] == "More deep work" for r in http.sheets.rows("Schedule"))
