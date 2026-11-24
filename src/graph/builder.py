from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from graph import nodes
from graph.state import AssistantState, Deps

# START -> load_context -> agent -(tool calls?)-> tools -> agent ... -> finalize -> END
RECURSION_LIMIT = 24


def build_graph(checkpointer: Any):
    builder = StateGraph(AssistantState, context_schema=Deps)
    builder.add_node("load_context", nodes.load_context)
    builder.add_node("agent", nodes.agent)
    builder.add_node("tools", nodes.tools_node)
    builder.add_node("finalize", nodes.finalize)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "agent")
    builder.add_conditional_edges("agent", nodes.route_after_agent, {"tools": "tools", "finalize": "finalize"})
    builder.add_edge("tools", "agent")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)
