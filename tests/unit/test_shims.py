"""The Pyodide shims must behave like the native libraries for LangGraph's serializer.

Each test loads the shim under its real module name in a subprocess-free way by
swapping ``sys.modules`` and reloading the serializer module.
"""

import datetime as dt
import importlib
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

SHIMS = Path(__file__).resolve().parents[2] / "shims"


def _load_shim(name: str):
    spec = importlib.util.spec_from_file_location(
        f"shim_{name}", SHIMS / name / "__init__.py", submodule_search_locations=[str(SHIMS / name)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # lets the shim's relative imports resolve
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def serializers(monkeypatch):
    """Return (shim_serializer, native_serializer)."""
    import ormsgpack as native

    import langgraph.checkpoint.serde.jsonplus as jp

    native_serde = jp.JsonPlusSerializer()
    monkeypatch.setitem(sys.modules, "ormsgpack", _load_shim("ormsgpack"))
    shim_jp = importlib.reload(jp)
    shim_serde = shim_jp.JsonPlusSerializer()
    yield shim_serde, native_serde
    monkeypatch.setitem(sys.modules, "ormsgpack", native)
    importlib.reload(jp)


STATE = {
    "messages": [
        HumanMessage("سلام! What's the weather?", id="h1"),
        AIMessage("", id="a1", tool_calls=[{"name": "get_time", "args": {"tz": "America/Chicago"}, "id": "c1"}]),
        ToolMessage('{"now": "12:00"}', tool_call_id="c1", id="t1"),
        AIMessage("It is noon.", id="a2", additional_kwargs={"thought_signature": "abc"}),
    ],
    "tool_call_count": 1,
    "when": dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc),
    "uid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "pending": None,
    "nested": {"a": [1, 2, (3, 4)], "b": b"\x00\x01"},
}


def _normalize(value):
    if isinstance(value, tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def test_shim_round_trips_message_state(serializers):
    shim, _ = serializers
    back = shim.loads_typed(shim.dumps_typed(STATE))
    assert [type(m) for m in back["messages"]] == [type(m) for m in STATE["messages"]]
    assert back["messages"][1].tool_calls[0]["args"] == {"tz": "America/Chicago"}
    assert back["messages"][0].content == STATE["messages"][0].content
    assert back["messages"][3].additional_kwargs == {"thought_signature": "abc"}
    assert back["when"] == STATE["when"]
    assert back["uid"] == STATE["uid"]
    assert _normalize(back["nested"]) == _normalize(STATE["nested"])


@pytest.mark.parametrize("direction", ["shim->native", "native->shim"])
def test_shim_is_wire_compatible_with_native(serializers, direction):
    shim, native = serializers
    src, dst = (shim, native) if direction == "shim->native" else (native, shim)
    back = dst.loads_typed(src.dumps_typed(STATE))
    assert [m.content for m in back["messages"]] == [m.content for m in STATE["messages"]]
    assert back["when"] == STATE["when"]


def test_remove_message_round_trips(serializers):
    shim, _ = serializers
    back = shim.loads_typed(shim.dumps_typed({"messages": [RemoveMessage(id="h1")]}))
    assert isinstance(back["messages"][0], RemoveMessage)


def test_shim_raises_its_own_error_types():
    shim = _load_shim("ormsgpack")
    with pytest.raises(shim.MsgpackEncodeError):
        shim.packb(object())
    with pytest.raises(shim.MsgpackDecodeError):
        shim.unpackb(b"\xc1")


def test_xxhash_shim_digest_lengths():
    xx = _load_shim("xxhash")
    assert len(xx.xxh3_128_hexdigest("abc")) == 32
    assert len(xx.xxh3_64_hexdigest(b"abc")) == 16
    assert xx.xxh3_128("abc").hexdigest() == xx.xxh3_128_hexdigest("abc")


def test_uuid7_shim_is_version_7_and_time_ordered():
    compat = _load_shim("uuid_utils").compat
    a, b = compat.uuid7(timestamp=1, nanos=0), compat.uuid7(timestamp=2, nanos=0)
    assert a.version == 7 and b.version == 7
    assert a < b
