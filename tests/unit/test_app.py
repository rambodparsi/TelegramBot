"""End-to-end handling of Telegram updates with fake D1 + HTTP (real LangGraph + saver)."""

import pytest

import app
from config import Config, DEFAULT_MODELS
from fakes import FakeD1, FakeHttp, gemini_text

OWNER = 9


def _cfg(**over):
    base = dict(app_env="test", telegram_bot_token="T", telegram_webhook_secret="s", admin_token="a",
                gemini_api_key="G", authorized_user_ids=frozenset({OWNER}),
                models={k: v.split(",") for k, v in DEFAULT_MODELS.items()})
    return Config(**{**base, **over})


def _update(uid, text, user=OWNER, chat_type="private"):
    return {"update_id": uid, "message": {"message_id": uid, "text": text,
            "chat": {"id": 42, "type": chat_type}, "from": {"id": user, "is_bot": False}}}


@pytest.fixture
def env():
    db, http = FakeD1(), FakeHttp()
    return db, http, app.Services(_cfg(), db, http)


async def test_answers_text_and_persists(env):
    db, http, svc = env
    assert await app.handle_update(_update(1, "hello"), svc) == "answered"
    assert http.telegram("sendMessage")[-1]["text"] == "Hello!"
    assert db.rows("SELECT role, content FROM messages ORDER BY id") == [("user", "hello"), ("assistant", "Hello!")]
    assert db.rows("SELECT status FROM telegram_updates") == [("done",)]
    assert db.rows("SELECT count(*) FROM usage_events")[0][0] == 1


async def test_history_is_sent_on_next_turn_and_new_resets(env):
    db, http, svc = env
    await app.handle_update(_update(1, "my name is Rambod"), svc)
    await app.handle_update(_update(2, "what is my name?"), svc)
    contents = http.gemini_calls()[-1][1]["contents"]
    assert [c["parts"][0]["text"] for c in contents] == ["my name is Rambod", "Hello!", "what is my name?"]

    await app.handle_update(_update(3, "/new"), svc)
    await app.handle_update(_update(4, "fresh"), svc)
    assert [c["parts"][0]["text"] for c in http.gemini_calls()[-1][1]["contents"]] == ["fresh"]


async def test_duplicate_update_is_processed_once(env):
    _, http, svc = env
    assert await app.handle_update(_update(1, "hi"), svc) == "answered"
    assert await app.handle_update(_update(1, "hi"), svc) == "ignored:duplicate"
    assert len(http.gemini_calls()) == 1


async def test_strangers_and_groups_are_ignored(env):
    _, http, svc = env
    assert await app.handle_update(_update(1, "hi", user=666), svc) == "ignored:unauthorized"
    assert await app.handle_update(_update(2, "hi", chat_type="group"), svc) == "ignored:chat_type"
    assert http.calls == []


async def test_whoami_works_before_allowlist_is_configured():
    http = FakeHttp()
    svc = app.Services(_cfg(authorized_user_ids=frozenset()), FakeD1(), http)
    assert await app.handle_update(_update(1, "/whoami", user=123), svc) == "whoami:unconfigured"
    assert "123" in http.telegram("sendMessage")[0]["text"]
    assert await app.handle_update(_update(2, "hello", user=123), svc) == "ignored:unauthorized"


async def test_model_failure_sends_apology_and_marks_failed():
    from http_client import HttpResponse
    db, http = FakeD1(), FakeHttp(lambda m, b: HttpResponse(503, "{}"))
    svc = app.Services(_cfg(), db, http)
    status = await app.handle_update(_update(1, "hi"), svc)
    assert status.startswith("failed:")
    assert "something went wrong" in http.telegram("sendMessage")[-1]["text"]
    assert db.rows("SELECT status FROM telegram_updates") == [("failed",)]


async def test_state_is_trimmed_but_context_stays_coherent():
    db, http = FakeD1(), FakeHttp(lambda m, b: gemini_text("ok"))
    svc = app.Services(_cfg(max_state_messages=6, context_turns=2), db, http)
    for i in range(1, 8):
        await app.handle_update(_update(i, f"msg {i}"), svc)
    contents = http.gemini_calls()[-1][1]["contents"]
    assert contents[0]["role"] == "user" and contents[0]["parts"][0]["text"] == "msg 6"
    snap = await svc.graph.aget_state({"configurable": {"thread_id": db.rows("SELECT thread_id FROM conversations")[0][0]}})
    assert len(snap.values["messages"]) <= 6
