"""Minimal async helpers for the Cloudflare D1 binding.

Talks to ``env.DB`` directly (``prepare().bind().all()``) so the Worker never has to
import SQLAlchemy, which alone exhausts the Free plan's per-request CPU budget.
"""

from __future__ import annotations

from typing import Any, Sequence


class D1Error(RuntimeError):
    """A D1 statement failed."""


_JS_NULL: Any = None


def _js_null() -> Any:
    """JS ``null`` (Python ``None`` would cross the FFI as ``undefined``, which D1 rejects)."""
    global _JS_NULL
    if _JS_NULL is None:
        try:
            from js import JSON  # type: ignore[import-not-found]
        except ImportError:  # outside the Worker runtime (tests): plain None is fine
            return None
        _JS_NULL = JSON.parse("null")
    return _JS_NULL


def _to_py(value: Any) -> Any:
    if value is None:
        return None
    name = type(value).__name__
    if name in ("JsNull", "JsUndefined"):
        return None
    if hasattr(value, "to_py"):
        return value.to_py()
    return value


def _bind(db: Any, sql: str, params: Sequence[Any] | None) -> Any:
    stmt = db.prepare(sql)
    if params:
        stmt = stmt.bind(*[_js_null() if p is None else p for p in params])
    return stmt


async def query(db: Any, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Run a statement and return its rows as plain dicts."""
    try:
        result = await _bind(db, sql, params).all()
    except Exception as exc:
        raise D1Error(f"{type(exc).__name__}: {exc} | sql={sql[:120]}") from exc
    rows = _to_py(getattr(result, "results", None)) or []
    return [{k: _to_py(v) for k, v in row.items()} for row in rows]


async def first(db: Any, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    rows = await query(db, sql, params)
    return rows[0] if rows else None


async def execute(db: Any, sql: str, params: Sequence[Any] | None = None) -> None:
    try:
        await _bind(db, sql, params).run()
    except Exception as exc:
        raise D1Error(f"{type(exc).__name__}: {exc} | sql={sql[:120]}") from exc
