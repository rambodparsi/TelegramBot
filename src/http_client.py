"""Tiny HTTP abstraction so business logic is testable outside the Worker runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class HttpResponse:
    status: int
    text: str

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


class Http(Protocol):
    async def post_json(self, url: str, payload: Any, headers: dict[str, str] | None = None) -> HttpResponse: ...


class WorkerHttp:
    """Implementation backed by the Workers ``fetch`` API."""

    async def post_json(self, url: str, payload: Any, headers: dict[str, str] | None = None) -> HttpResponse:
        from workers import fetch  # imported lazily so tests never need the Workers SDK

        resp = await fetch(
            url,
            method="POST",
            headers={"content-type": "application/json", **(headers or {})},
            body=json.dumps(payload, ensure_ascii=False),
        )
        return HttpResponse(status=int(resp.status), text=await resp.text())
