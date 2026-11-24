"""Thin Gemini REST client with per-role model fallback."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from http_client import Http

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Status codes that mean "try the next model in the chain".
_FALLBACK_STATUSES = {404, 429, 500, 502, 503, 504}


class GeminiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class GeminiResult:
    text: str
    function_calls: list[dict[str, Any]]
    parts: list[dict[str, Any]]           # raw model parts (keeps thought signatures)
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    fallbacks: int = 0
    finish_reason: str | None = None
    attempts: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)   # from Google Search grounding
    search_queries: list[str] = field(default_factory=list)


class GeminiClient:
    def __init__(self, api_key: str, http: Http):
        self._key = api_key
        self._http = http

    async def generate(
        self,
        models: list[str],
        contents: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int = 2048,
        thinking_level: str | None = "low",
        temperature: float | None = None,
    ) -> GeminiResult:
        body: dict[str, Any] = {"contents": contents, "generationConfig": {"maxOutputTokens": max_output_tokens}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["generationConfig"]["temperature"] = temperature
        if thinking_level:
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": thinking_level}

        attempts: list[str] = []
        last_error: GeminiError | None = None
        for index, model in enumerate(models):
            try:
                result = await self._call(model, body)
            except GeminiError as exc:
                attempts.append(f"{model}:{exc.status}")
                last_error = exc
                if exc.status in _FALLBACK_STATUSES or exc.status is None:
                    continue
                raise
            result.fallbacks = index
            result.attempts = attempts
            return result
        raise GeminiError(
            f"all models failed ({', '.join(attempts)}): {last_error}",
            status=last_error.status if last_error else None,
            retryable=True,
        )

    async def _call(self, model: str, body: dict[str, Any]) -> GeminiResult:
        try:
            resp = await self._http.post_json(API.format(model=model), body, {"x-goog-api-key": self._key})
        except Exception as exc:  # network failure
            raise GeminiError(f"network: {exc}", status=None, retryable=True) from exc

        if resp.status == 400 and "thinking" in resp.text.lower() and "thinkingConfig" in body["generationConfig"]:
            # Model does not accept a thinking level: retry once without it.
            trimmed = {**body, "generationConfig": {k: v for k, v in body["generationConfig"].items() if k != "thinkingConfig"}}
            return await self._call(model, trimmed)

        if resp.status != 200:
            message = resp.text[:300]
            try:
                message = resp.json()["error"]["message"][:300]
            except Exception:
                pass
            raise GeminiError(f"{model} HTTP {resp.status}: {message}", status=resp.status,
                              retryable=resp.status in _FALLBACK_STATUSES)

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise GeminiError(f"{model} returned no answer ({reason})", status=200)
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        usage = data.get("usageMetadata") or {}
        grounding = candidate.get("groundingMetadata") or {}
        sources, seen = [], set()
        for chunk in grounding.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            uri = web.get("uri")
            if uri and uri not in seen:
                seen.add(uri)
                sources.append({"title": web.get("title") or web.get("domain") or uri, "url": uri})
        return GeminiResult(
            text=text.strip(),
            function_calls=calls,
            parts=parts,
            model=data.get("modelVersion") or model,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0),
            finish_reason=candidate.get("finishReason"),
            sources=sources,
            search_queries=list(grounding.get("webSearchQueries") or []),
        )
