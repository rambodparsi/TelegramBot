"""Minimal Google Sheets REST client: values.batchGet and values.batchUpdate."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from http_client import Http
from planner.google_auth import GoogleAuthError, ServiceAccountAuth
from planner.schema import READ_ONLY_TABS

BASE = "https://sheets.googleapis.com/v4/spreadsheets/{sid}"


class SheetsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _tab_of(a1: str) -> str:
    name = a1.split("!", 1)[0]
    return name[1:-1].replace("''", "'") if name.startswith("'") else name


class SheetsClient:
    def __init__(self, sheet_id: str, auth: ServiceAccountAuth, http: Http):
        self.sheet_id = sheet_id
        self._auth = auth
        self._http = http
        self.get_calls = 0
        self.update_calls = 0

    async def _send(self, method: str, url: str, body: str | None = None) -> Any:
        for attempt in (1, 2):
            token = await self._auth.token()
            headers = {"authorization": f"Bearer {token}"}
            if body is not None:
                headers["content-type"] = "application/json"
            try:
                resp = await self._http.request(method, url, body=body, headers=headers)
            except Exception as exc:
                raise SheetsError("Google Sheets request failed (network)", retryable=True) from exc
            if resp.status == 401 and attempt == 1:
                self._auth.invalidate()  # expired/revoked token: refresh once
                continue
            if resp.status != 200:
                message = ""
                try:
                    message = str(((resp.json() or {}).get("error") or {}).get("message", ""))[:300]
                except ValueError:
                    pass
                raise SheetsError(f"Google Sheets HTTP {resp.status}: {message}", status=resp.status,
                                  retryable=resp.status == 429 or resp.status >= 500)
            return resp.json() or {}
        raise SheetsError("Google Sheets authentication failed", status=401)

    async def batch_get(self, ranges: list[str]) -> dict[str, list[list[Any]]]:
        """Read ranges (UNFORMATTED_VALUE, serial dates). Keys are the requested range strings."""
        if not ranges:
            return {}
        query = "&".join(f"ranges={quote(r, safe='')}" for r in ranges)
        url = (BASE.format(sid=self.sheet_id) + f"/values:batchGet?{query}&valueRenderOption=UNFORMATTED_VALUE"
               "&dateTimeRenderOption=SERIAL_NUMBER&majorDimension=ROWS")
        self.get_calls += 1
        data = await self._send("GET", url)
        value_ranges = data.get("valueRanges") or []
        return {r: (value_ranges[i].get("values") or []) if i < len(value_ranges) else []
                for i, r in enumerate(ranges)}

    async def batch_update(self, data: list[tuple[str, list[list[Any]]]]) -> None:
        """Write ranges with USER_ENTERED. Refuses to touch view/config tabs."""
        if not data:
            return
        for a1, _ in data:
            if _tab_of(a1) in READ_ONLY_TABS:
                raise SheetsError(f"refusing to write to protected tab {_tab_of(a1)}")
        body = json.dumps({"valueInputOption": "USER_ENTERED", "includeValuesInResponse": False,
                           "data": [{"range": r, "majorDimension": "ROWS", "values": vals} for r, vals in data]},
                          ensure_ascii=False)
        self.update_calls += 1
        await self._send("POST", BASE.format(sid=self.sheet_id) + "/values:batchUpdate", body)


__all__ = ["GoogleAuthError", "SheetsClient", "SheetsError"]
