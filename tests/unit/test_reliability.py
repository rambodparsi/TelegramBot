"""Queue-mode behaviour: retries, outbox re-delivery, stale leases, cleanup, /status."""

import pytest

import app
from fakes import FakeD1, FakeHttp, gemini_text
from http_client import HttpResponse
from telegram.client import TelegramError
from test_app import _cfg, _update


class Flaky:
    def __init__(self, failures, status=503):
        self.failures, self.status, self.calls = failures, status, 0

    def __call__(self, model, body):
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            return HttpResponse(self.status, '{"error":{"message":"busy"}}')
        return gemini_text("answer")


async def test_temporary_model_outage_retries_then_succeeds():
    flaky = Flaky(failures=3)  # all three chat models fail on attempt 1
    db, http = FakeD1(), FakeHttp(flaky)
    svc = app.Services(_cfg(), db, http)
    with pytest.raises(app.RetryLater) as wait:
        await app.handle_update(_update(1, "hi"), svc, attempt=1, max_attempts=4)
    assert wait.value.delay == app.RETRY_DELAYS[0]
    assert db.rows("SELECT status, attempts FROM telegram_updates") == [("retry", 1)]
    assert http.telegram("sendMessage") == []  # no apology while a retry is pending

    assert await app.handle_update(_update(1, "hi"), svc, attempt=2, max_attempts=4) == "answered:text"
    assert db.rows("SELECT status, attempts FROM telegram_updates") == [("done", 2)]
    assert [m["text"] for m in http.telegram("sendMessage")] == ["answer"]


async def test_last_attempt_reports_busy_to_user():
    db, http = FakeD1(), FakeHttp(Flaky(failures=99, status=429))
    svc = app.Services(_cfg(), db, http)
    status = await app.handle_update(_update(1, "hi"), svc, attempt=4, max_attempts=4)
    assert status.startswith("failed:")
    assert "busy or out of free quota" in http.telegram("sendMessage")[-1]["text"]


async def test_send_failure_redelivers_stored_reply_without_rerunning_model():
    db, flaky = FakeD1(), Flaky(failures=0)
    http = FakeHttp(flaky)
    svc = app.Services(_cfg(), db, http)

    real_send, fail = svc.telegram.send_message, {"n": 1}

    async def send_once_failing(*a, **k):
        if fail["n"]:
            fail["n"] -= 1
            raise TelegramError("sendMessage", 502, "Bad Gateway")
        return await real_send(*a, **k)

    svc.telegram.send_message = send_once_failing
    with pytest.raises(app.RetryLater):
        await app.handle_update(_update(1, "hi"), svc, attempt=1, max_attempts=4)
    assert flaky.calls == 1
    assert await app.handle_update(_update(1, "hi"), svc, attempt=2, max_attempts=4) == "answered:text"
    assert flaky.calls == 1  # model not called again
    thread = db.rows("SELECT thread_id FROM conversations")[0][0]
    state = await svc.graph.aget_state({"configurable": {"thread_id": thread}})
    assert [m.type for m in state.values["messages"]] == ["human", "ai"]  # turn not duplicated
    assert db.rows("SELECT role FROM messages ORDER BY id") == [("user",), ("assistant",)]
    assert db.rows("SELECT response, user_text FROM telegram_updates") == [(None, None)]  # outbox cleared


async def test_stale_processing_lease_can_be_reclaimed_but_fresh_one_cannot():
    db = FakeD1()
    svc = app.Services(_cfg(), db, FakeHttp())
    db.conn.execute("INSERT INTO telegram_updates (update_id, chat_id, status) VALUES (1, 42, 'processing')")
    assert await app.handle_update(_update(1, "hi"), svc) == "ignored:duplicate"
    db.conn.execute("UPDATE telegram_updates SET updated_at = datetime('now', '-10 minutes')")
    assert await app.handle_update(_update(1, "hi"), svc) == "answered:text"


async def test_status_command_and_cleanup():
    db, http = FakeD1(), FakeHttp()
    svc = app.Services(_cfg(), db, http)
    await app.handle_update(_update(1, "hi"), svc)
    await app.handle_update(_update(2, "/status"), svc)
    text = http.telegram("sendMessage")[-1]["text"]
    assert "Messages: 2" in text and "gemini-test" in text
    db.conn.execute("UPDATE telegram_updates SET received_at = datetime('now', '-40 days')")
    await svc.repo.cleanup()
    assert db.rows("SELECT count(*) FROM telegram_updates")[0][0] == 0
