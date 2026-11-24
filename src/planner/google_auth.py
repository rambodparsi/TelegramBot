"""Google service-account OAuth for the Workers runtime (RS256 JWT signed with Web Crypto).

No Python crypto packages: signing uses ``crypto.subtle`` from the Worker runtime.
Tokens are cached per isolate and refreshed 60 s before expiry. Never log tokens or keys.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Callable, Protocol
from urllib.parse import quote

from http_client import Http

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_TOKENS: dict[str, tuple[str, float]] = {}  # client_email -> (token, expires_at)


class GoogleAuthError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Signer(Protocol):
    async def sign_rs256(self, data: bytes) -> bytes: ...


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class WebCryptoSigner:
    """RSASSA-PKCS1-v1_5 / SHA-256 via the Workers Web Crypto API."""

    def __init__(self, private_key_pem: str):
        body = "".join(line for line in private_key_pem.strip().splitlines() if "-----" not in line)
        self._der = base64.b64decode(body)
        self._key: Any = None

    async def sign_rs256(self, data: bytes) -> bytes:
        from js import Object, Uint8Array, crypto  # type: ignore[import-not-found]
        from pyodide.ffi import to_js  # type: ignore[import-not-found]

        algo = to_js({"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"}, dict_converter=Object.fromEntries)
        if self._key is None:
            self._key = await crypto.subtle.importKey("pkcs8", to_js(self._der), algo, False, to_js(["sign"]))
        signature = await crypto.subtle.sign(algo, self._key, to_js(data))
        return bytes(Uint8Array.new(signature).to_py())


class ServiceAccountAuth:
    def __init__(self, service_account_json: str, http: Http, signer: Signer | None = None,
                 clock: Callable[[], float] = time.time):
        try:
            info = json.loads(service_account_json)
            self.client_email: str = info["client_email"]
            private_key: str = info["private_key"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GoogleAuthError("GOOGLE_SERVICE_ACCOUNT_JSON is not a valid service-account key") from exc
        self.token_uri: str = info.get("token_uri") or DEFAULT_TOKEN_URI
        self._http = http
        self._signer = signer or WebCryptoSigner(private_key)
        self._clock = clock

    def invalidate(self) -> None:
        _TOKENS.pop(self.client_email, None)

    async def token(self) -> str:
        now = self._clock()
        cached = _TOKENS.get(self.client_email)
        if cached and cached[1] - 60 > now:
            return cached[0]
        header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = b64url(json.dumps({"iss": self.client_email, "scope": SCOPE, "aud": self.token_uri,
                                    "iat": int(now), "exp": int(now) + 3600}, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}"
        signature = await self._signer.sign_rs256(signing_input.encode("ascii"))
        assertion = f"{signing_input}.{b64url(signature)}"
        body = "grant_type=" + quote("urn:ietf:params:oauth:grant-type:jwt-bearer", safe="") + "&assertion=" + assertion
        try:
            resp = await self._http.request("POST", self.token_uri, body=body,
                                            headers={"content-type": "application/x-www-form-urlencoded"})
        except Exception as exc:
            raise GoogleAuthError("token request failed (network)", retryable=True) from exc
        if resp.status != 200:
            raise GoogleAuthError(f"token request failed (HTTP {resp.status})", status=resp.status,
                                  retryable=resp.status == 429 or resp.status >= 500)
        data = resp.json() or {}
        token = data.get("access_token")
        if not token:
            raise GoogleAuthError("token response had no access_token", status=resp.status)
        _TOKENS[self.client_email] = (token, now + float(data.get("expires_in") or 3600))
        return token
