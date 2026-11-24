"""Graph nodes. Functions of (state, runtime context)."""

from __future__ import annotations

from typing import Sequence

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph import END
from langgraph.runtime import Runtime

from graph.state import AssistantState, Deps
from llm.convert import to_ai_message, to_contents
from prompts import render_system_prompt
from tools import registry
from tools.executor import run_tool_call
from tools.registry import ToolContext

FALLBACK_REPLY = "Sorry — I couldn't produce an answer just now. Please try again."


def recent_window(messages: Sequence[AnyMessage], turns: int) -> list[AnyMessage]:
    """Last ``turns`` user turns (and everything after them), starting at a HumanMessage."""
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if not starts:
        return list(messages)
    first = starts[-turns] if len(starts) >= turns else starts[0]
    return list(messages[first:])


def _first_step_of_turn(messages: Sequence[AnyMessage]) -> bool:
    return bool(messages) and isinstance(messages[-1], HumanMessage)


async def load_context(state: AssistantState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    if deps.repo is not None and deps.user_id:
        deps.profile = await deps.repo.profile(deps.user_id)
    tz_pref = deps.profile.get("preferences", {}).get("timezone")
    if deps.planner is not None and tz_pref:
        deps.planner.set_timezone(tz_pref)
    return {}


async def agent(state: AssistantState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    window = recent_window(state["messages"], deps.context_turns)
    contents = to_contents(window)
    role = "chat"
    if deps.attachments and _first_step_of_turn(state["messages"]) and contents and contents[-1]["role"] == "user":
        # Media goes only into the current turn's request; history keeps the text marker.
        contents[-1] = {"role": "user", "parts": [*deps.attachments, *contents[-1]["parts"]]}
        role = "vision"
    # Once the per-turn tool budget is spent, stop offering tools so the model must answer.
    tools = registry.declarations(deps.tools) if state.get("tool_call_count", 0) < deps.max_tool_calls else None
    result = await deps.llm.generate(
        deps.models[role], contents, system=render_system_prompt(deps.profile, deps.default_timezone, planner_enabled=deps.planner is not None),
        tools=tools, max_output_tokens=2048,
    )
    deps.usage.append({
        "role": role, "model": result.model, "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens, "fallbacks": result.fallbacks,
    })
    message = to_ai_message(result.text, result.function_calls, result.parts, f"{deps.run_id}-{len(state['messages'])}")
    return {"messages": [message]}


def route_after_agent(state: AssistantState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else "finalize"


async def tools_node(state: AssistantState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    last = state["messages"][-1]
    used = state.get("tool_call_count", 0)
    ctx = ToolContext(repo=deps.repo, user_id=deps.user_id, update_id=deps.update_id, profile=deps.profile,
                      default_timezone=deps.default_timezone, llm=deps.llm, models=deps.models, usage=deps.usage,
                      http=deps.http, tavily_api_key=deps.tavily_api_key, search_provider=deps.search_provider,
                      planner=deps.planner, chat_id=deps.chat_id, buttons=deps.buttons, undo_ops=deps.undo_ops,
                      tools=deps.tools)
    results: list[ToolMessage] = []
    for call in last.tool_calls:
        if used >= deps.max_tool_calls:
            # Every function call must get a response, or the next Gemini request is invalid.
            results.append(ToolMessage('{"error": "tool budget for this message is used up"}',
                                       tool_call_id=call["id"], name=call["name"], status="error"))
            continue
        used += 1
        results.append(await run_tool_call(call, ctx))
    return {"messages": results, "tool_call_count": used}


def finalize(state: AssistantState, runtime: Runtime[Deps]) -> dict:
    deps = runtime.context
    messages = state["messages"]
    last = messages[-1]
    text = last.content if isinstance(last, AIMessage) and isinstance(last.content, str) else ""
    update: dict = {"final_response": text.strip() or FALLBACK_REPLY}

    # Keep checkpointed state small: full history lives in the app `messages` table.
    # Cut only at a user turn so tool calls are never separated from their results.
    if len(messages) > deps.max_state_messages:
        keep_from = len(messages) - deps.max_state_messages
        starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and i >= keep_from]
        cut = starts[0] if starts else keep_from
        update["messages"] = [RemoveMessage(id=m.id) for m in messages[:cut] if m.id]
    return update


__all__ = ["END", "agent", "finalize", "load_context", "route_after_agent", "tools_node"]
