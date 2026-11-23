import app
from fakes import FakeD1, FakeHttp, gemini_text
from test_app import OWNER, _cfg, _update


def _callback(uid, data, message_id=500):
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "data": data, "from": {"id": OWNER},
                                                  "message": {"message_id": message_id, "chat": {"id": 42, "type": "private"}}}}


def _buttons(http):
    markup = http.telegram("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0]
    return {b["text"]: b["callback_data"] for b in markup}


async def _setup():
    db, http = FakeD1(), FakeHttp(lambda m, b: gemini_text("hi"))
    svc = app.Services(_cfg(), db, http)
    await app.handle_update(_update(1, "hello"), svc)
    return db, http, svc


async def test_delete_history_requires_confirmation_and_removes_checkpoints():
    db, http, svc = await _setup()
    assert db.rows("SELECT count(*) FROM checkpoints")[0][0] > 0
    await app.handle_update(_update(2, "/deletehistory"), svc)
    assert db.rows("SELECT count(*) FROM messages")[0][0] == 2  # nothing deleted yet

    assert await app.handle_update(_callback(3, _buttons(http)["Yes, delete"]), svc) == "callback:delete_history"
    for table in ("messages", "conversations", "checkpoints", "writes"):
        assert db.rows(f"SELECT count(*) FROM {table}")[0][0] == 0, table
    assert "Deleted 1 conversation" in http.telegram("editMessageText")[-1]["text"]


async def test_confirmation_is_single_use_and_cancel_deletes_nothing():
    db, http, svc = await _setup()
    await app.handle_update(_update(2, "/deletehistory"), svc)
    buttons = _buttons(http)
    assert await app.handle_update(_callback(3, buttons["Cancel"]), svc) == "callback:cancel:delete_history"
    assert await app.handle_update(_callback(4, buttons["Yes, delete"]), svc) == "callback:invalid"
    assert db.rows("SELECT count(*) FROM messages")[0][0] == 2


async def test_forged_or_expired_tokens_are_rejected():
    db, http, svc = await _setup()
    assert await app.handle_update(_callback(2, "ok:made-up"), svc) == "callback:invalid"
    await app.handle_update(_update(3, "/forget"), svc)
    db.conn.execute("UPDATE pending_actions SET expires_at = datetime('now', '-1 minute')")
    assert await app.handle_update(_callback(4, _buttons(http)["Yes, delete"]), svc) == "callback:invalid"


async def test_forget_all_wipes_memory_and_preferences():
    db, http, svc = await _setup()
    await svc.repo.add_fact(OWNER, "likes tea")
    await svc.repo.set_preference(OWNER, "name", "Rambod")
    await app.handle_update(_update(2, "/forget"), svc)
    assert await app.handle_update(_callback(3, _buttons(http)["Yes, delete"]), svc) == "callback:forget_all"
    assert db.rows("SELECT count(*) FROM memory_facts")[0][0] == 0
    assert db.rows("SELECT count(*) FROM user_preferences")[0][0] == 0


async def test_strangers_cannot_press_buttons():
    db, http, svc = await _setup()
    await app.handle_update(_update(2, "/deletehistory"), svc)
    stranger = _callback(3, _buttons(http)["Yes, delete"])
    stranger["callback_query"]["from"]["id"] = 666
    assert await app.handle_update(stranger, svc) == "ignored:unauthorized"
    assert db.rows("SELECT count(*) FROM messages")[0][0] == 2
