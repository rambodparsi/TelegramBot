"""Cloudflare Worker entrypoint.

Milestone 0: only health and benchmark probes are exposed.
"""

from __future__ import annotations

import hmac
import traceback
from urllib.parse import parse_qs, urlparse

from workers import Response, WorkerEntrypoint

import bench


def _authorized(request, env) -> bool:
    expected = getattr(env, "BENCH_TOKEN", None)
    given = request.headers.get("x-bench-token") or ""
    return bool(expected) and hmac.compare_digest(given, str(expected))


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        path = url.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(url.query).items()}

        if path == "/health":
            return Response.json({"ok": True, "env": getattr(self.env, "APP_ENV", "?")})

        if not path.startswith("/bench/"):
            return Response("Not found", status=404)
        if not _authorized(request, self.env):
            return Response("Not found", status=404)

        thread = q.get("thread", "bench-default")
        text = q.get("text", "What time is it in Chicago?")
        try:
            if path == "/bench/noop":
                out = {"ok": True}
            elif path == "/bench/graph-int":
                out = await bench.graph_int(self.env.DB, thread)
            elif path == "/bench/graph-messages":
                out = await bench.graph_messages(self.env.DB, thread, text)
            elif path == "/bench/gemini":
                out = await bench.graph_gemini(
                    self.env.DB,
                    thread,
                    text,
                    str(self.env.GEMINI_API_KEY),
                    q.get("model", "gemini-3.5-flash-lite"),
                )
            elif path == "/bench/checkpoint-size":
                out = await bench.checkpoint_size(self.env.DB, thread)
            else:
                return Response("Not found", status=404)
            return Response.json(out)
        except BaseException as exc:  # surface errors to the benchmark runner
            return Response.json(
                {"error": type(exc).__name__, "detail": str(exc)[:500],
                 "trace": traceback.format_exc()[-3000:]},
                status=500,
            )
