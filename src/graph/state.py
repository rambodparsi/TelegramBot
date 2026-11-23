from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AssistantState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    current_input: dict[str, Any]          # normalized Telegram input (no bytes)
    tool_call_count: int
    final_response: str | None


@dataclass
class Deps:
    """Per-run runtime context (never checkpointed)."""

    llm: Any                                # GeminiClient
    models: dict[str, list[str]]
    system_prompt: str
    context_turns: int = 20
    max_state_messages: int = 40
    run_id: str = "run"
    # Media for the *current* turn only (Gemini parts). Never checkpointed.
    attachments: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
