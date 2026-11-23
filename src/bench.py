"""Milestone 0 feasibility probes.

Each probe exercises one layer of the stack so its CPU cost can be read from
Cloudflare's analytics. Probes are protected by the BENCH_TOKEN secret and are
removed before production use.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from workers import fetch

from memory.checkpointer import D1CheckpointSaver

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

async def get_saver(db: Any) -> D1CheckpointSaver:
    saver = D1CheckpointSaver(db)
    await saver.setup()
    return saver


# --- probe: official int graph ------------------------------------------------


async def graph_int(db: Any, thread: str) -> dict:
    builder = StateGraph(int)
    builder.add_node("add_one", lambda s: s + 1)
    builder.add_edge(START, "add_one")
    builder.add_edge("add_one", END)
    graph = builder.compile(checkpointer=await get_saver(db))
    result = await graph.ainvoke(3, {"configurable": {"thread_id": thread}})
    return {"result": result}


# --- probe: message graph with a tool loop (no network) ----------------------


class BenchState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_call_count: int


def _fake_agent(state: BenchState) -> dict:
    last = state["messages"][-1]
    if isinstance(last, HumanMessage):
        call = {"name": "get_current_datetime", "args": {"tz": "America/Chicago"}, "id": "c1"}
        return {"messages": [AIMessage(content="", tool_calls=[call])]}
    return {"messages": [AIMessage(content=f"Done. History has {len(state['messages'])} msgs.")]}


def _fake_tools(state: BenchState) -> dict:
    last = state["messages"][-1]
    out = [
        ToolMessage(content=json.dumps({"now": "2026-11-22T12:00:00-06:00"}), tool_call_id=c["id"])
        for c in last.tool_calls
    ]
    return {"messages": out, "tool_call_count": state.get("tool_call_count", 0) + len(out)}


def _route(state: BenchState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END


def _build_loop_graph(agent_fn, saver):
    builder = StateGraph(BenchState)
    builder.add_node("agent", agent_fn)
    builder.add_node("tools", _fake_tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", _route, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=saver)


async def graph_messages(db: Any, thread: str, text: str) -> dict:
    graph = _build_loop_graph(_fake_agent, await get_saver(db))
    result = await graph.ainvoke(
        {"messages": [HumanMessage(text)]}, {"configurable": {"thread_id": thread}}
    )
    msgs = result["messages"]
    return {
        "message_count": len(msgs),
        "types": [type(m).__name__ for m in msgs[-4:]],
        "last": msgs[-1].content,
    }


# --- probe: same graph, real Gemini call -------------------------------------


def _to_gemini(messages: list[AnyMessage]) -> list[dict]:
    contents = []
    for m in messages:
        if isinstance(m, HumanMessage):
            contents.append({"role": "user", "parts": [{"text": m.content}]})
        elif isinstance(m, AIMessage) and m.content:
            contents.append({"role": "model", "parts": [{"text": m.content}]})
    return contents[-20:]


def make_gemini_agent(api_key: str, model: str):
    async def agent(state: BenchState) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": "You are a concise assistant."}]},
            "contents": _to_gemini(state["messages"]),
            "generationConfig": {"maxOutputTokens": 200},
        }
        resp = await fetch(
            GEMINI_URL.format(model=model),
            method="POST",
            headers={"content-type": "application/json", "x-goog-api-key": api_key},
            body=json.dumps(body),
        )
        data = json.loads(await resp.text())
        if resp.status != 200:
            raise RuntimeError(f"gemini {resp.status}: {str(data)[:200]}")
        parts = data["candidates"][0]["content"].get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return {"messages": [AIMessage(content=text or "(empty)")]}

    return agent


async def graph_gemini(db: Any, thread: str, text: str, api_key: str, model: str) -> dict:
    graph = _build_loop_graph(make_gemini_agent(api_key, model), await get_saver(db))
    result = await graph.ainvoke(
        {"messages": [HumanMessage(text)]}, {"configurable": {"thread_id": thread}}
    )
    return {"message_count": len(result["messages"]), "reply": result["messages"][-1].content}


# --- probe: checkpoint size ---------------------------------------------------


async def checkpoint_size(db: Any, thread: str) -> dict:
    row = await (
        db.prepare(
            "SELECT length(checkpoint) AS bytes FROM checkpoints WHERE thread_id = ? "
            "ORDER BY checkpoint_id DESC LIMIT 1"
        )
        .bind(thread)
        .first()
    )
    count = await (
        db.prepare("SELECT count(*) AS n FROM checkpoints WHERE thread_id = ?").bind(thread).first()
    )
    to_py = getattr(row, "to_py", None)
    return {
        "latest_checkpoint_bytes": (row.to_py() if to_py else row)["bytes"] if row else None,
        "checkpoint_rows": (count.to_py() if hasattr(count, "to_py") else count)["n"],
    }
