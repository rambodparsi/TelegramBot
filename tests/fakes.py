"""Test doubles for the Worker runtime: D1 (backed by sqlite3) and HTTP."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from http_client import BytesResponse, HttpResponse

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class _Stmt:
    def __init__(self, conn: sqlite3.Connection, sql: str, params: tuple = ()):
        self.conn, self.sql, self.params = conn, sql, params

    def bind(self, *params):
        return _Stmt(self.conn, self.sql, params)

    async def all(self):
        cur = self.conn.execute(self.sql, self.params)
        cols = [d[0] for d in cur.description or []]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        self.conn.commit()
        return SimpleNamespace(results=rows)

    async def run(self):
        self.conn.execute(self.sql, self.params)
        self.conn.commit()
        return SimpleNamespace(success=True)


class FakeD1:
    def __init__(self, migrate: bool = True):
        self.conn = sqlite3.connect(":memory:")
        if migrate:
            for f in sorted(MIGRATIONS.glob("*.sql")):
                self.conn.executescript(f.read_text())

    def prepare(self, sql: str) -> _Stmt:
        return _Stmt(self.conn, sql)

    def rows(self, sql: str) -> list[tuple]:
        return self.conn.execute(sql).fetchall()


def gemini_text(text: str, model: str = "gemini-test") -> HttpResponse:
    return HttpResponse(200, json.dumps({
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        "modelVersion": model,
    }))


class FakeHttp:
    """Routes requests by URL; records everything."""

    def __init__(self, gemini: Callable[[str, dict], HttpResponse] | None = None):
        self.calls: list[tuple[str, Any]] = []
        self._gemini = gemini or (lambda model, body: gemini_text("Hello!"))
        self._next_id = 100

    async def post_json(self, url: str, payload: Any, headers: dict[str, str] | None = None) -> HttpResponse:
        self.calls.append((url, payload))
        m = re.search(r"/models/([^:]+):generateContent", url)
        if m:
            return self._gemini(m.group(1), payload)
        if url.endswith("/getFile"):
            return HttpResponse(200, json.dumps({"ok": True, "result": {"file_path": f"files/{payload['file_id']}"}}))
        if "api.telegram.org" in url:
            self._next_id += 1
            return HttpResponse(200, json.dumps({"ok": True, "result": {"message_id": self._next_id}}))
        raise AssertionError(f"unexpected URL {url}")

    files: dict[str, bytes] = {}

    async def get_bytes(self, url: str) -> BytesResponse:
        self.calls.append((url, None))
        name = url.rsplit("/", 1)[-1]
        if name not in self.files:
            return BytesResponse(404, b"")
        return BytesResponse(200, self.files[name])

    def telegram(self, method: str) -> list[dict]:
        return [p for u, p in self.calls if u.endswith("/" + method)]

    def gemini_calls(self) -> list[tuple[str, dict]]:
        return [(u, p) for u, p in self.calls if ":generateContent" in u]
