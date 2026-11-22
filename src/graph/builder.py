from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from graph import nodes
from graph.state import AssistantState, Deps


def build_graph(checkpointer: Any):
    builder = StateGraph(AssistantState, context_schema=Deps)
    builder.add_node("agent", nodes.agent)
    builder.add_node("finalize", nodes.finalize)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)
