"""LangChain messages <-> Gemini ``contents``."""

from __future__ import annotations

import json
from typing import Any, Sequence

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

SIGNATURE_KEY = "gemini_parts"  # AIMessage.additional_kwargs key holding raw model parts


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content)


def to_contents(messages: Sequence[AnyMessage]) -> list[dict[str, Any]]:
    """Convert history to Gemini contents. System messages are skipped (sent separately)."""
    contents: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, HumanMessage):
            parts = [{"text": _text(m.content)}]
            contents.append({"role": "user", "parts": parts})
        elif isinstance(m, AIMessage):
            raw = m.additional_kwargs.get(SIGNATURE_KEY)
            if raw:
                # Replay the model's own parts verbatim (preserves thought signatures).
                parts = [p for p in raw if not p.get("thought")]
            else:
                parts = []
                if _text(m.content):
                    parts.append({"text": _text(m.content)})
                for call in m.tool_calls:
                    parts.append({"functionCall": {"name": call["name"], "args": call["args"]}})
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif isinstance(m, ToolMessage):
            try:
                response = json.loads(_text(m.content))
                if not isinstance(response, dict):
                    response = {"result": response}
            except ValueError:
                response = {"result": _text(m.content)}
            part = {"functionResponse": {"name": m.name or "tool", "response": response}}
            if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
    return contents


def to_ai_message(text: str, function_calls: list[dict[str, Any]], parts: list[dict[str, Any]], call_id_prefix: str) -> AIMessage:
    tool_calls = [
        {"name": c["name"], "args": c.get("args") or {}, "id": f"{call_id_prefix}-{i}"}
        for i, c in enumerate(function_calls)
    ]
    kept_parts = [p for p in parts if not p.get("thought")]
    return AIMessage(content=text, tool_calls=tool_calls, additional_kwargs={SIGNATURE_KEY: kept_parts})
