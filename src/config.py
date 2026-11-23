"""Typed access to Worker vars and secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODEL_ROLES = ("chat", "vision", "transcribe", "light", "deep")

DEFAULT_MODELS = {
    "chat": "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    "vision": "gemini-3.5-flash,gemini-3.5-flash-lite",
    "transcribe": "gemini-3.5-transcribe,gemini-3.5-flash-lite",
    "light": "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    "deep": "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash",
}


class ConfigError(RuntimeError):
    pass


def _get(env: Any, name: str) -> str | None:
    try:
        value = getattr(env, name)
    except AttributeError:
        return None
    if value is None or type(value).__name__ in ("JsNull", "JsUndefined"):
        return None
    value = str(value).strip()
    return value or None


def _parse_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(p) for p in raw.replace(";", ",").replace(" ", ",").split(",") if p.strip())


@dataclass(frozen=True)
class Config:
    app_env: str
    telegram_bot_token: str
    telegram_webhook_secret: str | None
    admin_token: str | None
    gemini_api_key: str
    authorized_user_ids: frozenset[int]
    timezone: str = "America/Chicago"
    models: dict[str, list[str]] = field(default_factory=dict)
    context_turns: int = 20
    max_state_messages: int = 40
    max_media_bytes: int = 10 * 1024 * 1024

    def model_chain(self, role: str) -> list[str]:
        return self.models[role]


def load_config(env: Any) -> Config:
    missing = [n for n in ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY") if not _get(env, n)]
    if missing:
        raise ConfigError(f"missing secrets: {', '.join(missing)}")
    models = {
        role: [m.strip() for m in (_get(env, f"MODEL_{role.upper()}") or DEFAULT_MODELS[role]).split(",") if m.strip()]
        for role in MODEL_ROLES
    }
    return Config(
        app_env=_get(env, "APP_ENV") or "dev",
        telegram_bot_token=_get(env, "TELEGRAM_BOT_TOKEN") or "",
        telegram_webhook_secret=_get(env, "TELEGRAM_WEBHOOK_SECRET"),
        admin_token=_get(env, "ADMIN_TOKEN") or _get(env, "BENCH_TOKEN"),
        gemini_api_key=_get(env, "GEMINI_API_KEY") or "",
        authorized_user_ids=_parse_ids(_get(env, "AUTHORIZED_TELEGRAM_USER_IDS")),
        timezone=_get(env, "DEFAULT_TIMEZONE") or "America/Chicago",
        models=models,
        context_turns=int(_get(env, "CONTEXT_TURNS") or 20),
        max_state_messages=int(_get(env, "MAX_STATE_MESSAGES") or 40),
        max_media_bytes=int(float(_get(env, "MAX_MEDIA_MB") or 10) * 1024 * 1024),
    )
