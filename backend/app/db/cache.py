"""Small SQLite-backed key/value cache with a TTL (Spec 6.4: caching layer, TTL from config).

Lives in the same DB file as store.py (default_db_path(); SCREENING_DB_PATH overrides) but
uses its own table, so store.py is untouched. Stdlib sqlite3 only.

Used as a seam around the *default* fetch functions of the evidence collectors:
`cached_fetch(inner)` wraps a fetch and stores only (status, body) keyed by URL. Request
headers (and therefore any token) never reach this module's storage. Only definitive
answers (200, 404) are cached; rate limits, 5xx and transport errors are never cached, so a
transient failure cannot be replayed as if it were data.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..config import cfg
from .store import default_db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_cache (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    stored_at REAL NOT NULL
);
"""
CACHEABLE_STATUSES = frozenset({200, 404})


class Cache:
    def __init__(
        self,
        path: str | Path | None = None,
        ttl_hours: float | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = float(ttl_hours if ttl_hours is not None else cfg("cache.ttl_hours", 24)) * 3600
        self._clock = clock
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self, key: str) -> tuple[bool, Any]:
        """Returns (hit, value). An entry older than the TTL is a miss and is dropped."""
        with self._conn() as c:
            row = c.execute(
                "SELECT value_json, stored_at FROM evidence_cache WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return False, None
            if self._clock() - float(row[1]) >= self.ttl_seconds:
                c.execute("DELETE FROM evidence_cache WHERE key=?", (key,))
                return False, None
            return True, json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO evidence_cache(key, value_json, stored_at) VALUES (?,?,?)",
                (key, json.dumps(value), self._clock()),
            )

    def purge_expired(self) -> int:
        cutoff = self._clock() - self.ttl_seconds
        with self._conn() as c:
            return c.execute("DELETE FROM evidence_cache WHERE stored_at<=?", (cutoff,)).rowcount


def cached_fetch(inner: Callable[..., Awaitable[tuple]], cache: Cache | None = None) -> Callable[..., Awaitable[tuple]]:
    """Wrap a fetch `(url, *rest) -> (status, body, ...)` so repeat GETs of a URL hit the cache.

    Only `url` is the key and only `(status, body)` is stored; `rest` (headers, with any
    token) is passed through to `inner` and never persisted. A hit returns `(status, body)`."""
    store = cache or Cache()

    async def fetch(url: str, *rest: Any) -> tuple:
        hit, value = store.get(url)
        if hit:
            return int(value[0]), value[1]
        result = await inner(url, *rest)
        status, body = result[0], result[1]
        if status in CACHEABLE_STATUSES:
            try:
                store.set(url, [status, body])
            except (TypeError, ValueError, sqlite3.Error):
                pass  # an unserialisable body or a locked DB must never break collection
        return result

    return fetch


def cache_enabled() -> bool:
    return bool(cfg("cache.enabled", True))
