"""Graph nodes. Pure functions of (state, runtime context)."""

from __future__ import annotations

from typing import Sequence

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage
from langgraph.runtime import Runtime

from graph.state import AssistantState, Deps
from llm.convert import to_ai_message, to_contents

FALLBACK_REPLY = "Sorry — I couldn't produce an answer just now. Please try again."


def recent_window(messages: Sequence[AnyMessage], turns: int) -> list[AnyMessage]:
    """Last ``turns`` user turns (and everything after them), starting at a HumanMessage."""
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if not starts:
        return list(messages)
    first = starts[-turns] if len(starts) >= turns else starts[0]
    return list(messages[first:])


async def agent(state: AssistantState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    window = recent_window(state["messages"], deps.context_turns)
    result = await deps.llm.generate(
        deps.models["chat"], to_contents(window), system=deps.system_prompt, max_output_tokens=2048
    )
    deps.usage.append({
        "role": "chat", "model": result.model, "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens, "fallbacks": result.fallbacks,
    })
    message = to_ai_message(result.text, result.function_calls, result.parts, f"{deps.run_id}-{len(state['messages'])}")
    return {"messages": [message]}


def finalize(state: AssistantState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    messages = state["messages"]
    last = messages[-1]
    text = last.content if isinstance(last, AIMessage) and isinstance(last.content, str) else ""
    update: dict = {"final_response": text.strip() or FALLBACK_REPLY}

    # Keep checkpointed state small: full history lives in the app `messages` table.
    if len(messages) > deps.max_state_messages:
        keep_from = len(messages) - deps.max_state_messages
        starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and i >= keep_from]
        cut = starts[0] if starts else keep_from
        update["messages"] = [RemoveMessage(id=m.id) for m in messages[:cut] if m.id]
    return update
