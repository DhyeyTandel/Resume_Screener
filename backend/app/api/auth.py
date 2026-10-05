"""Opt-in API key authentication (ASSUMPTIONS A-28).

With SCREENING_API_KEYS unset or blank the service is open, exactly as before. When it is set,
every /v1/* request needs a valid key except GET /v1/health. The key travels only in a header
(`Authorization: Bearer <key>` or `X-API-Key`), never in a query string, so it cannot leak into
access logs. Keys are compared as SHA-256 digests with hmac.compare_digest. Only the key's label
is ever recorded (audit entries); the key itself is never stored, logged or echoed.

Entry formats, comma separated:
  label:key               plaintext key, hashed at load time
  label:sha256:<64 hex>   store only the hash
  sha256:<64 hex>         hash with an automatic label (key-1, key-2, ...)
The check is a pure ASGI middleware so an unauthenticated request is refused before any body
(for example a large multipart upload) is read.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import unicodedata

from fastapi.responses import JSONResponse

ENV_KEYS = "SCREENING_API_KEYS"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
PUBLIC = {("GET", "/v1/health"), ("HEAD", "/v1/health")}


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


def _label(text: str) -> str:
    clean = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf")).strip()
    return clean[:40] or "key"


def _configured() -> str:
    return os.environ.get(ENV_KEYS, "").strip()


def auth_required() -> bool:
    """True whenever SCREENING_API_KEYS is non-blank. A malformed value still locks the API
    (it fails closed) rather than silently leaving it open."""
    return bool(_configured())


def load_keys() -> list[tuple[str, bytes]]:
    """Parse the env var into (label, sha256 digest) pairs. Malformed entries are skipped."""
    out: list[tuple[str, bytes]] = []
    for n, raw in enumerate(_configured().split(","), start=1):
        entry = raw.strip()
        if not entry or ":" not in entry:
            continue
        head, _, rest = entry.partition(":")
        if head.lower() == "sha256" and _HEX64.match(rest.strip()):
            out.append((f"key-{n}", bytes.fromhex(rest.strip())))
            continue
        rest = rest.strip()
        if not rest:
            continue
        lower = rest.lower()
        if lower.startswith("sha256:") and _HEX64.match(rest[7:].strip()):
            out.append((_label(head), bytes.fromhex(rest[7:].strip())))
        else:
            out.append((_label(head), _digest(rest)))
    return out


def _presented(headers: list[tuple[bytes, bytes]]) -> list[str]:
    found: list[str] = []
    for name, value in headers:
        lname = name.lower()
        text = value.decode("latin-1").strip()
        if lname == b"authorization":
            scheme, _, token = text.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                found.append(token.strip())
        elif lname == b"x-api-key" and text:
            found.append(text)
    return found


def authenticate(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Label of the matching key, or None. Every stored digest is compared, no early exit."""
    keys = load_keys()
    for token in _presented(headers):
        probe = _digest(token)
        match: str | None = None
        for label, stored in keys:
            if hmac.compare_digest(probe, stored) and match is None:
                match = label
        if match is not None:
            return match
    return None


def _is_protected(method: str, path: str) -> bool:
    if path != "/v1" and not path.startswith("/v1/"):
        return False
    return method != "OPTIONS" and (method, path) not in PUBLIC


class ApiKeyMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or not auth_required()
            or not _is_protected(scope["method"], scope["path"])
        ):
            await self.app(scope, receive, send)
            return
        label = authenticate(scope["headers"])
        if label is None:
            resp = JSONResponse(
                status_code=401,
                content={
                    "code": "UNAUTHORIZED",
                    "message": "A valid API key is required.",
                    "remediation": (
                        "Send your key as 'Authorization: Bearer <key>' or an 'X-API-Key' "
                        "header. Keys in the URL are not accepted."
                    ),
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
            await resp(scope, receive, send)
            return
        scope.setdefault("state", {})["actor"] = label
        await self.app(scope, receive, send)
