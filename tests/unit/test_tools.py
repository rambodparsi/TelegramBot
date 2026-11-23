import json

import pytest

import app
from fakes import FakeD1, FakeHttp
from http_client import HttpResponse
from test_app import OWNER, _cfg, _update
from tools.registry import ToolInputError, validate, TOOLS


def _model(parts):
    return HttpResponse(200, json.dumps({"candidates": [{"content": {"role": "model", "parts": parts}}],
                                         "usageMetadata": {}, "modelVersion": "m"}))


def call(name, **args):
    return _model([{"functionCall": {"name": name, "args": args}, "thoughtSignature": "sig-" + name}])


def text(t):
    return _model([{"text": t}])


class Script:
    """Gemini fake that replays a scripted list of responses and records requests."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, model, body):
        self.requests.append(body)
        return self.responses.pop(0)


def _svc(script, **cfg):
    db, http = FakeD1(), FakeHttp(script)
    return db, http, app.Services(_cfg(**cfg), db, http)


def _tool_results(body):
    return [p["functionResponse"] for c in body["contents"] for p in c["parts"] if "functionResponse" in p]


async def test_save_preference_then_answer_grounded_in_result():
    script = Script(call("save_user_preference", key="answer_style", value="short"), text("Saved — I'll keep it short."))
    db, http, svc = _svc(script)
    assert await app.handle_update(_update(1, "From now on keep answers short"), svc) == "answered:text"
    assert db.rows("SELECT key, value FROM user_preferences") == [("answer_style", "short")]
    second = script.requests[1]
    assert _tool_results(second)[0]["response"] == {"saved": True, "key": "answer_style", "value": "short"}
    # the model's function-call part is replayed with its thought signature
    assert any(p.get("thoughtSignature") == "sig-save_user_preference" for c in second["contents"] for p in c["parts"])
    assert "answer_style: short" in second["systemInstruction"]["parts"][0]["text"]
    assert http.telegram("sendMessage")[-1]["text"] == "Saved — I'll keep it short."
    assert db.rows("SELECT tool_name, status FROM tool_executions") == [("save_user_preference", "ok")]


async def test_memories_are_in_next_turn_prompt_and_forget_by_id():
    script = Script(call("remember_fact", fact="User is allergic to peanuts."), text("Noted."), text("ok"))
    db, http, svc = _svc(script)
    await app.handle_update(_update(1, "Remember that I'm allergic to peanuts"), svc)
    await app.handle_update(_update(2, "what should I avoid?"), svc)
    assert "1: User is allergic to peanuts." in script.requests[-1]["systemInstruction"]["parts"][0]["text"]

    await app.handle_update(_update(3, "/memory"), svc)
    assert "allergic to peanuts" in http.telegram("sendMessage")[-1]["text"]
    await app.handle_update(_update(4, "/forget 1"), svc)
    assert db.rows("SELECT count(*) FROM memory_facts")[0][0] == 0


async def test_invalid_arguments_are_rejected_not_executed():
    script = Script(call("save_user_preference", key="password", value="x"), text("I can't save that."))
    db, _, svc = _svc(script)
    await app.handle_update(_update(1, "save my password"), svc)
    assert db.rows("SELECT count(*) FROM user_preferences")[0][0] == 0
    assert "must be one of" in _tool_results(script.requests[1])[0]["response"]["error"]
    assert db.rows("SELECT status FROM tool_executions") == [("rejected",)]


async def test_unknown_tool_and_bad_timezone():
    script = Script(call("run_shell", cmd="rm -rf /"), call("get_current_datetime", timezone="Mars/Base"), text("ok"))
    _, _, svc = _svc(script)
    await app.handle_update(_update(1, "hi"), svc)
    assert "unknown tool" in _tool_results(script.requests[1])[0]["response"]["error"]
    assert "unknown timezone" in _tool_results(script.requests[2])[-1]["response"]["error"]


async def test_tool_budget_stops_the_loop():
    script = Script(*[call("get_current_datetime") for _ in range(4)], text("It's noon."))
    _, http, svc = _svc(script)
    assert await app.handle_update(_update(1, "time?"), svc) == "answered:text"
    assert len(script.requests) == 5
    assert "tools" in script.requests[0] and "tools" not in script.requests[-1]  # tools withdrawn at budget
    assert http.telegram("sendMessage")[-1]["text"] == "It's noon."


async def test_tool_count_resets_each_turn():
    script = Script(*[call("get_current_datetime") for _ in range(4)], text("a"), call("get_current_datetime"), text("b"))
    _, _, svc = _svc(script)
    await app.handle_update(_update(1, "one"), svc)
    await app.handle_update(_update(2, "two"), svc)
    assert "tools" in script.requests[5]  # fresh budget on the second turn


async def test_datetime_uses_saved_timezone():
    script = Script(call("save_user_preference", key="timezone", value="Asia/Tehran"), text("ok"),
                    call("get_current_datetime"), text("ok"))
    _, _, svc = _svc(script)
    await app.handle_update(_update(1, "my timezone is Tehran"), svc)
    await app.handle_update(_update(2, "what time is it?"), svc)
    assert _tool_results(script.requests[3])[-1]["response"]["timezone"] == "Asia/Tehran"


def test_validator_edge_cases():
    schema = TOOLS["remember_fact"].parameters
    with pytest.raises(ToolInputError):
        validate(schema, {})
    with pytest.raises(ToolInputError):
        validate(schema, {"fact": "x" * 301})
    with pytest.raises(ToolInputError):
        validate(schema, {"fact": "ok", "extra": 1})
    assert validate(TOOLS["forget_fact"].parameters, {"id": 3.0}) == {"id": 3}


def test_declarations_only_use_gemini_safe_keywords():
    from tools.registry import declarations
    allowed = {"type", "description", "enum", "properties", "required", "items"}
    for decl in declarations()[0]["functionDeclarations"]:
        for prop in decl["parameters"]["properties"].values():
            assert set(prop) <= allowed, (decl["name"], prop)
