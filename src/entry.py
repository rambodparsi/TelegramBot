"""Cloudflare Worker entrypoint.

Routes
  POST /telegram/webhook      Telegram updates (secret-header protected)
  GET  /health                liveness
  POST /admin/setup-webhook   register webhook + bot commands   (x-admin-token)
  GET  /admin/webhook-info    Telegram's view of the webhook    (x-admin-token)
  GET  /bench/*               Milestone 0 probes                (x-bench-token)

All heavy imports are at module top level on purpose: they are captured in the
deploy-time memory snapshot instead of being billed to a request's CPU time.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import traceback
from urllib.parse import parse_qs, urlparse

from pyodide.ffi import create_proxy
from workers import Response, WorkerEntrypoint

import app
import bench
from config import ConfigError, load_config
from http_client import WorkerHttp

log = logging.getLogger("assistant")

_services: dict[int, app.Services] = {}


def _check_token(given: str | None, expected: str | None) -> bool:
    return bool(expected) and hmac.compare_digest((given or "").encode(), str(expected).encode())


class Default(WorkerEntrypoint):
    def _services(self) -> app.Services:
        key = id(self.env)
        svc = _services.get(key)
        if svc is None:
            svc = app.Services(load_config(self.env), self.env.DB, WorkerHttp())
            _services.clear()
            _services[key] = svc
        return svc

    def _background(self, coro) -> None:
        """Run work after the response is sent (Telegram gets its 200 immediately)."""
        task = asyncio.ensure_future(coro)
        proxy = create_proxy(task)
        task.add_done_callback(lambda _t: proxy.destroy())
        self.ctx.waitUntil(proxy)

    async def fetch(self, request):
        url = urlparse(request.url)
        path = url.path.rstrip("/") or "/"
        method = request.method.upper()

        if path == "/health":
            return Response.json({"ok": True, "env": getattr(self.env, "APP_ENV", "?")})

        if path == "/telegram/webhook" and method == "POST":
            return await self._webhook(request)

        if path.startswith("/admin/"):
            return await self._admin(request, path, url)

        if path.startswith("/bench/"):
            return await self._bench(request, path, url)

        return Response("Not found", status=404)

    async def _webhook(self, request):
        try:
            svc = self._services()
        except ConfigError as exc:
            log.error("config error: %s", exc)
            return Response("Service unavailable", status=503)
        if not _check_token(request.headers.get("x-telegram-bot-api-secret-token"), svc.cfg.telegram_webhook_secret):
            return Response("Not found", status=404)
        try:
            update = json.loads(await request.text())
        except ValueError:
            return Response("Bad request", status=400)

        async def run():
            try:
                status = await app.handle_update(update, svc)
                log.info("update %s -> %s", update.get("update_id"), status)
            except Exception:
                log.error("unhandled error: %s", traceback.format_exc()[-1500:])

        self._background(run())
        return Response("ok")

    async def _admin(self, request, path, url):
        try:
            svc = self._services()
        except ConfigError as exc:
            return Response.json({"error": str(exc)}, status=503)
        if not _check_token(request.headers.get("x-admin-token"), svc.cfg.admin_token):
            return Response("Not found", status=404)
        try:
            if path == "/admin/setup-webhook" and request.method.upper() == "POST":
                if not svc.cfg.telegram_webhook_secret:
                    return Response.json({"error": "TELEGRAM_WEBHOOK_SECRET is not set"}, status=400)
                hook = f"{url.scheme}://{url.netloc}/telegram/webhook"
                await svc.telegram.set_webhook(hook, svc.cfg.telegram_webhook_secret)
                await svc.telegram.set_my_commands(app.BOT_COMMANDS)
                await svc.telegram.set_my_commands(app.BOT_COMMANDS_FA, "fa")
                return Response.json({"ok": True, "webhook": hook})
            if path == "/admin/webhook-info":
                return Response.json(await svc.telegram.get_webhook_info())
        except Exception as exc:
            return Response.json({"error": f"{type(exc).__name__}: {exc}"}, status=502)
        return Response("Not found", status=404)

    async def _bench(self, request, path, url):
        if not _check_token(request.headers.get("x-bench-token"), getattr(self.env, "BENCH_TOKEN", None)):
            return Response("Not found", status=404)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
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
                out = await bench.graph_gemini(self.env.DB, thread, text, str(self.env.GEMINI_API_KEY),
                                               q.get("model", "gemini-3.5-flash-lite"))
            elif path == "/bench/checkpoint-size":
                out = await bench.checkpoint_size(self.env.DB, thread)
            else:
                return Response("Not found", status=404)
            return Response.json(out)
        except BaseException as exc:  # surface errors to the benchmark runner
            return Response.json({"error": type(exc).__name__, "detail": str(exc)[:500],
                                  "trace": traceback.format_exc()[-3000:]}, status=500)
