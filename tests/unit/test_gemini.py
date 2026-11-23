import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fakes import FakeHttp, gemini_text
from http_client import HttpResponse
from llm.convert import SIGNATURE_KEY, to_ai_message, to_contents
from llm.gemini import GeminiClient, GeminiError


async def test_falls_back_on_overload_and_quota():
    def gemini(model, body):
        return {"a": HttpResponse(503, '{"error":{"message":"busy"}}'),
                "b": HttpResponse(429, '{"error":{"message":"quota"}}')}.get(model) or gemini_text("ok", model)

    result = await GeminiClient("k", FakeHttp(gemini)).generate(["a", "b", "c"], [])
    assert (result.text, result.model, result.fallbacks) == ("ok", "c", 2)


async def test_does_not_fall_back_on_bad_request():
    client = GeminiClient("k", FakeHttp(lambda m, b: HttpResponse(400, '{"error":{"message":"bad"}}')))
    with pytest.raises(GeminiError) as err:
        await client.generate(["a", "b"], [])
    assert err.value.status == 400


async def test_retries_without_thinking_config_when_rejected():
    seen = []

    def gemini(model, body):
        seen.append("thinkingConfig" in body["generationConfig"])
        if body["generationConfig"].get("thinkingConfig"):
            return HttpResponse(400, '{"error":{"message":"Thinking level is not supported"}}')
        return gemini_text("ok")

    assert (await GeminiClient("k", FakeHttp(gemini)).generate(["a"], [])).text == "ok"
    assert seen == [True, False]


def test_contents_round_trip_tool_calls_and_signatures():
    parts = [{"functionCall": {"name": "t", "args": {"x": 1}}, "thoughtSignature": "sig"}]
    ai = to_ai_message("", [{"name": "t", "args": {"x": 1}}], parts, "r1")
    assert ai.tool_calls[0]["id"] == "r1-0" and ai.additional_kwargs[SIGNATURE_KEY] == parts
    contents = to_contents([HumanMessage("q"), ai, ToolMessage(json.dumps({"v": 2}), tool_call_id="r1-0", name="t"),
                            AIMessage("done")])
    assert [c["role"] for c in contents] == ["user", "model", "user", "model"]
    assert contents[1]["parts"][0]["thoughtSignature"] == "sig"
    assert contents[2]["parts"][0]["functionResponse"] == {"name": "t", "response": {"v": 2}}
