"""Run model-requested tool calls safely."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import ToolMessage

from tools.registry import TOOLS, ToolContext, ToolInputError, args_hash, validate

log = logging.getLogger("assistant")


async def run_tool_call(call: dict[str, Any], ctx: ToolContext) -> ToolMessage:
    name, raw_args, call_id = call.get("name", ""), call.get("args") or {}, call["id"]
    tool = TOOLS.get(name)
    digest = args_hash(raw_args if isinstance(raw_args, dict) else {"_": str(raw_args)})
    if tool is None:
        await ctx.repo.log_tool(ctx.update_id, name or "?", digest, "rejected", "unknown tool")
        return _msg(call_id, name, {"error": f"unknown tool '{name}'"})
    try:
        args = validate(tool.parameters, raw_args)
        result = await tool.handler(ctx, args)
        await ctx.repo.log_tool(ctx.update_id, name, digest, "ok")
        return _msg(call_id, name, result)
    except ToolInputError as exc:
        await ctx.repo.log_tool(ctx.update_id, name, digest, "rejected", str(exc))
        return _msg(call_id, name, {"error": str(exc)})
    except Exception as exc:  # a failed write must never look like success
        log.error("tool %s failed: %s: %s", name, type(exc).__name__, exc)
        await ctx.repo.log_tool(ctx.update_id, name, digest, "error", f"{type(exc).__name__}: {exc}")
        return _msg(call_id, name, {"error": "the tool failed; nothing was saved"})


def _msg(call_id: str, name: str, payload: dict[str, Any]) -> ToolMessage:
    return ToolMessage(json.dumps(payload, ensure_ascii=False), tool_call_id=call_id, name=name,
                       status="error" if "error" in payload else "success")
