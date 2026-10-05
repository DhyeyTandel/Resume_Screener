"""Abuse limits (A-28 items 2 and 3): per-client rate limiting and a bounded screening queue.

Both are in-process. They protect one worker; with several workers each enforces its own
budget, and a shared store would be needed to make the limits global.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from ..config import cfg

# --------------------------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------------------------

# Routes that start LLM work or heavy parsing. Everything else is a cheap read.
EXPENSIVE_ROUTES = frozenset(
    {
        ("POST", "/v1/screenings"),
        ("POST", "/v1/authenticity/assess"),
        ("POST", "/v1/skills/analyze"),
        ("POST", "/v1/interview-questions"),
    }
)


def error_body(code: str, message: str, remediation: str | None = None) -> dict[str, Any]:
    """Same shape as routes.err(): {code, message, remediation}. Mirrored here instead of
    imported so this module does not depend on the router."""
    body: dict[str, Any] = {"code": code, "message": message}
    if remediation:
        body["remediation"] = remediation
    return body


class TokenBucket:
    __slots__ = ("tokens", "stamp")

    def __init__(self, tokens: float, stamp: float) -> None:
        self.tokens, self.stamp = tokens, stamp


class BucketTable:
    """Token buckets keyed by (client, class), bounded to `max_entries` with LRU eviction so a
    flood of distinct source addresses cannot grow memory without limit."""

    def __init__(self, max_entries: int) -> None:
        self.max_entries = max(1, int(max_entries))
        self._d: OrderedDict[tuple[str, str], TokenBucket] = OrderedDict()

    def __len__(self) -> int:
        return len(self._d)

    def take(self, key: tuple[str, str], now: float, rate: float, burst: float) -> float:
        """Spend one token. Returns 0.0 when allowed, else seconds until a token is available."""
        b = self._d.get(key)
        if b is None:
            b = TokenBucket(burst, now)
            self._d[key] = b
            while len(self._d) > self.max_entries:
                self._d.popitem(last=False)
        else:
            self._d.move_to_end(key)
            b.tokens = min(burst, b.tokens + max(0.0, now - b.stamp) * rate)
            b.stamp = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return 0.0
        return (1.0 - b.tokens) / rate if rate > 0 else 3600.0


def _valid_ip(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return None


class RateLimitMiddleware:
    """Pure ASGI middleware. Budgets come from `limits.rate` in config.yaml unless passed in;
    `limits.rate.enabled: false` turns it off (read per request); `clock` is injectable for deterministic tests.

    The client is the socket peer address. X-Forwarded-For is ignored unless `trust_proxy` is
    set; then the rightmost entry is used, because that is the one the trusted proxy itself
    appended (earlier entries are client-supplied and spoofable)."""

    def __init__(
        self,
        app,
        *,
        expensive_per_min: float | None = None,
        expensive_burst: float | None = None,
        read_per_min: float | None = None,
        read_burst: float | None = None,
        trust_proxy: bool | None = None,
        max_clients: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        def pick(v, key, default):
            return v if v is not None else cfg(f"limits.rate.{key}", default)

        self.app = app
        self.clock = clock
        self.trust_proxy = bool(pick(trust_proxy, "trust_proxy", False))
        self.budgets = {
            "expensive": (
                float(pick(expensive_per_min, "expensive_per_min", 10)) / 60.0,
                float(pick(expensive_burst, "expensive_burst", 5)),
            ),
            "read": (
                float(pick(read_per_min, "read_per_min", 300)) / 60.0,
                float(pick(read_burst, "read_burst", 100)),
            ),
        }
        self.table = BucketTable(int(pick(max_clients, "max_clients", 10000)))

    def client_id(self, scope) -> str:
        peer = (scope.get("client") or ("unknown", 0))[0]
        if self.trust_proxy:
            for name, value in scope.get("headers", []):
                if name == b"x-forwarded-for":
                    last = value.decode("latin-1").split(",")[-1]
                    ip = _valid_ip(last)
                    if ip:
                        return ip
        return str(peer)

    @staticmethod
    def classify(scope) -> str:
        path = scope.get("path", "").rstrip("/") or "/"
        return "expensive" if (scope.get("method", ""), path) in EXPENSIVE_ROUTES else "read"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not cfg("limits.rate.enabled", True):
            await self.app(scope, receive, send)
            return
        klass = self.classify(scope)
        rate, burst = self.budgets[klass]
        wait = self.table.take((self.client_id(scope), klass), self.clock(), rate, burst)
        if wait <= 0:
            await self.app(scope, receive, send)
            return
        retry = max(1, math.ceil(wait))
        body = json.dumps(
            error_body(
                "RATE_LIMITED",
                "Too many requests from this address.",
                f"Wait {retry} seconds and try again, or slow down the request rate.",
            )
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(retry).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------------------------
# Bounded screening queue
# --------------------------------------------------------------------------------------------


class ScreeningLimiter:
    """At most `slots` candidate screenings run at once, and at most `max_pending` candidates
    may be admitted (running or waiting) at any time. Sizes are read from `limits.queue` on
    first use and can be changed with `configure()`.

    The semaphore is created per event loop (an asyncio primitive must not outlive its loop),
    so the same limiter works across test clients and server restarts."""

    def __init__(self) -> None:
        self._slots: int | None = None
        self._max_pending: int | None = None
        self.pending = 0
        self.active = 0
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def configure(self, slots: int | None = None, max_pending: int | None = None) -> None:
        self._slots, self._max_pending = slots, max_pending
        self._sem = self._loop = None

    @property
    def slots(self) -> int:
        return max(1, int(self._slots if self._slots is not None
                          else cfg("limits.queue.screening_slots", 4)))

    @property
    def max_pending(self) -> int:
        return max(1, int(self._max_pending if self._max_pending is not None
                          else cfg("limits.queue.max_pending_candidates", 100)))

    @property
    def retry_after_s(self) -> int:
        return max(1, int(cfg("limits.queue.retry_after_s", 15)))

    def reset(self) -> None:
        self.pending = self.active = 0
        self._sem = self._loop = None

    def try_admit(self, n_candidates: int) -> bool:
        """Reserve room for a screening of n candidates. False means reject it up front. A
        screening larger than the whole cap is admitted only into an empty queue, so a
        misconfigured cap cannot make it permanently unscreenable."""
        n = max(0, int(n_candidates))
        if self.pending > 0 and self.pending + n > self.max_pending:
            return False
        self.pending += n
        return True

    def release_admission(self, n_candidates: int) -> None:
        """Give back what try_admit reserved. Call once per admitted screening, in a finally."""
        self.pending = max(0, self.pending - max(0, int(n_candidates)))

    @asynccontextmanager
    async def slot(self):
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            self._sem, self._loop = asyncio.Semaphore(self.slots), loop
        sem = self._sem
        async with sem:
            self.active += 1
            try:
                yield
            finally:
                self.active -= 1

    def rejection_response(self):
        """503 with Retry-After and the standard error body, for create_screening to return."""
        from starlette.responses import JSONResponse

        retry = self.retry_after_s
        return JSONResponse(
            status_code=503,
            content=error_body(
                "QUEUE_FULL",
                "The screening queue is full.",
                f"Try again in about {retry} seconds, or submit fewer candidates.",
            ),
            headers={"Retry-After": str(retry)},
        )


screening_slots = ScreeningLimiter()


def screening_slot():
    """`async with screening_slot():` around one candidate's screening."""
    return screening_slots.slot()


def try_admit(n_candidates: int) -> bool:
    return screening_slots.try_admit(n_candidates)


def release_admission(n_candidates: int) -> None:
    screening_slots.release_admission(n_candidates)
