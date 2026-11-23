import base64

import pytest

import app
from fakes import FakeD1, FakeHttp, gemini_text
from test_app import OWNER, _cfg

AUDIO = b"OggS-fake-audio"
IMAGE = b"\xff\xd8\xff-fake-jpeg"


def _msg(uid, **payload):
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": 42, "type": "private"},
                                          "from": {"id": OWNER, "is_bot": False}, **payload}}


def _voice(uid, size=len(AUDIO), mime="audio/ogg"):
    return _msg(uid, voice={"file_id": "voice1", "file_unique_id": "u1", "mime_type": mime, "file_size": size})


def _photo(uid, caption=None):
    extra = {"caption": caption} if caption else {}
    return _msg(uid, photo=[{"file_id": "small", "file_size": 10}, {"file_id": "photo1", "file_size": len(IMAGE)}], **extra)


def _parts(body):
    return body["contents"][-1]["parts"]


def _router(transcript="remind me to buy milk", answer="Sure!"):
    seen = []

    def gemini(model, body):
        seen.append((model, body))
        if any("Transcribe this audio" in p.get("text", "") for p in body["contents"][-1]["parts"]):
            return gemini_text(transcript, model)
        return gemini_text(answer, model)

    return gemini, seen


@pytest.fixture
def setup():
    gemini, seen = _router()
    http = FakeHttp(gemini)
    http.files = {"voice1": AUDIO, "photo1": IMAGE}
    db = FakeD1()
    return db, http, seen, app.Services(_cfg(), db, http)


async def test_voice_is_transcribed_then_answered(setup):
    db, http, seen, svc = setup
    assert await app.handle_update(_voice(1), svc) == "answered:voice"
    (t_model, t_body), (c_model, c_body) = seen
    assert "transcribe" in t_model
    assert _parts(t_body)[0]["inline_data"] == {"mime_type": "audio/ogg", "data": base64.b64encode(AUDIO).decode()}
    assert c_model == "gemini-3.5-flash"
    assert _parts(c_body) == [{"text": "[Voice message] remind me to buy milk"}]  # no audio re-sent
    sent = http.telegram("sendMessage")[-1]["text"]
    assert sent.startswith("🎙️ <i>remind me to buy milk</i>") and sent.endswith("Sure!")
    assert db.rows("SELECT kind FROM messages WHERE role='user'") == [("voice",)]
    assert {r[0] for r in db.rows("SELECT role FROM usage_events")} == {"transcribe", "chat"}


async def test_empty_transcript_asks_to_repeat_without_calling_chat():
    gemini, seen = _router(transcript="")
    http = FakeHttp(gemini)
    http.files = {"voice1": AUDIO}
    svc = app.Services(_cfg(), FakeD1(), http)
    assert await app.handle_update(_voice(1), svc) == "voice:empty"
    assert [m for m, _ in seen] == ["gemini-3.5-transcribe", "gemini-3.5-flash-lite"]  # chat never called
    assert "couldn't make out" in http.telegram("sendMessage")[-1]["text"]


async def test_photo_goes_to_vision_model_once_and_history_keeps_marker(setup):
    db, http, seen, svc = setup
    assert await app.handle_update(_photo(1, caption="what is this?"), svc) == "answered:photo"
    model, body = seen[-1]
    assert model == "gemini-3.5-flash"
    parts = _parts(body)
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
    assert parts[1] == {"text": "[Photo] what is this?"}

    await app.handle_update(_msg(2, text="and the total?"), svc)
    follow_up = seen[-1][1]["contents"]
    assert all("inline_data" not in p for c in follow_up for p in c["parts"])  # image not resent
    assert follow_up[0]["parts"] == [{"text": "[Photo] what is this?"}]


async def test_oversized_and_unsupported_files_are_rejected_before_download(setup):
    _, http, seen, svc = setup
    assert await app.handle_update(_voice(1, size=50 * 1024 * 1024), svc) == "rejected:voice"
    assert "too large" in http.telegram("sendMessage")[-1]["text"]
    assert await app.handle_update(_voice(2, mime="audio/x-weird"), svc) == "rejected:voice"
    assert "can't read this file type" in http.telegram("sendMessage")[-1]["text"]
    assert not http.telegram("getFile") and not seen


async def test_unsupported_message_type(setup):
    _, http, _, svc = setup
    assert await app.handle_update(_msg(1, sticker={"file_id": "s"}), svc) == "unsupported"
