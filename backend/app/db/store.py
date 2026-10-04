"""SQLite persistence (stdlib sqlite3 only).

The audit_log table is append-only by construction: this module exposes only
insert (append_audit) and select (list_audit) for it. LLM prompts are never
stored; report JSON is stored exactly as produced.
"""
from __future__ import annotations
import datetime
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..config import cfg

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screenings (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    done INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    screening_id TEXT NOT NULL,
    report_json TEXT NOT NULL,
    row_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_screening ON candidates(screening_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_candidate ON audit_log(candidate_id);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def default_db_path() -> Path:
    override = os.getenv("SCREENING_DB_PATH")
    if override:
        return Path(override)
    return Path(cfg("app.data_dir", "./data")) / "screening.db"


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # A screening still "processing" when the store opens was cut off by a
            # restart; nothing resumes it, so mark it rather than let the UI poll forever.
            c.execute("UPDATE screenings SET status='interrupted' WHERE status='processing'")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self._conn()
        try:
            with conn:
                return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # screenings
    def create_screening(self, sid: str, total: int, status: str = "processing") -> None:
        self._run(
            "INSERT INTO screenings (id, status, total, done, created_at) VALUES (?,?,?,0,?)",
            (sid, status, total, _now()),
        )

    def update_screening(self, sid: str, *, status: str, done: int) -> None:
        self._run("UPDATE screenings SET status=?, done=? WHERE id=?", (status, done, sid))

    def get_screening(self, sid: str) -> dict | None:
        rows = self._run("SELECT * FROM screenings WHERE id=?", (sid,))
        if not rows:
            return None
        s = rows[0]
        cands = self._run(
            "SELECT row_json FROM candidates WHERE screening_id=? ORDER BY rowid", (sid,)
        )
        return {
            "screening_id": s["id"],
            "status": s["status"],
            "total": s["total"],
            "done": s["done"],
            "candidates": [json.loads(r["row_json"]) for r in cands],
        }

    # candidates
    def save_candidate(self, cid: str, sid: str, report: dict, row: dict) -> None:
        self._run(
            "INSERT OR REPLACE INTO candidates (id, screening_id, report_json, row_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (cid, sid, json.dumps(report), json.dumps(row), _now()),
        )

    def get_candidate(self, cid: str) -> dict | None:
        rows = self._run("SELECT report_json FROM candidates WHERE id=?", (cid,))
        return json.loads(rows[0]["report_json"]) if rows else None

    # audit log: insert and select only
    def append_audit(self, candidate_id: str, event: str, payload: dict[str, Any]) -> dict:
        at = _now()
        self._run(
            "INSERT INTO audit_log (candidate_id, event, payload_json, at) VALUES (?,?,?,?)",
            (candidate_id, event, json.dumps(payload), at),
        )
        return {"candidate_id": candidate_id, "event": event, "payload": payload, "at": at}

    def list_audit(self, candidate_id: str | None = None, event: str | None = None) -> list[dict]:
        sql, params, where = "SELECT * FROM audit_log", [], []
        if candidate_id is not None:
            where.append("candidate_id=?")
            params.append(candidate_id)
        if event is not None:
            where.append("event=?")
            params.append(event)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        return [
            {
                "id": r["id"],
                "candidate_id": r["candidate_id"],
                "event": r["event"],
                "payload": json.loads(r["payload_json"]),
                "at": r["at"],
            }
            for r in self._run(sql, tuple(params))
        ]


_stores: dict[str, Store] = {}


def get_store() -> Store:
    """Return a store for the current default path (re-resolved each call so
    SCREENING_DB_PATH changes, e.g. in tests, take effect)."""
    key = str(default_db_path())
    if key not in _stores:
        _stores[key] = Store(key)
    return _stores[key]
