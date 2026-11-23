from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AssistantState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    current_input: dict[str, Any]          # normalized Telegram input (no bytes)
    tool_call_count: int                   # reset to 0 by every new user turn
    final_response: str | None


@dataclass
class Deps:
    """Per-run runtime context (never checkpointed)."""

    llm: Any                                # GeminiClient
    models: dict[str, list[str]]
    repo: Any = None                        # storage.repo.Repo
    user_id: int = 0
    update_id: int | None = None
    default_timezone: str = "America/Chicago"
    context_turns: int = 20
    max_state_messages: int = 40
    max_tool_calls: int = 4
    run_id: str = "run"
    # Media for the *current* turn only (Gemini parts). Never checkpointed.
    attachments: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=lambda: {"preferences": {}, "facts": []})
