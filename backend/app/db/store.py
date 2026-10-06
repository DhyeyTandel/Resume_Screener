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
CREATE TABLE IF NOT EXISTS authenticity_reports (
    candidate_id TEXT PRIMARY KEY,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_candidate ON audit_log(candidate_id);
CREATE TABLE IF NOT EXISTS notes (
    audit_id INTEGER PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_candidate ON notes(candidate_id);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


# Tables that carry an `owner` column (the API-key label that created the row, A-30).
# Added by an additive migration so DB files written by older versions keep working.
_OWNED_TABLES = ("screenings", "candidates", "authenticity_reports")
_OWNER_KEYS = {"screenings": "id", "candidates": "id", "authenticity_reports": "candidate_id"}


def _migrate(c: sqlite3.Connection) -> None:
    """Idempotent: ALTER TABLE ADD COLUMN only when PRAGMA table_info shows it missing."""
    for table in _OWNED_TABLES:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if "owner" not in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN owner TEXT")
            except sqlite3.OperationalError:  # another process added it between check and alter
                pass


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
            _migrate(c)
            # A screening still "processing" when the store opens was cut off by a
            # restart; nothing resumes it, so mark it rather than let the UI poll forever.
            c.execute("UPDATE screenings SET status='interrupted' WHERE status='processing'")
        self.purge_expired()

    def purge_expired(self, days: float | None = None) -> dict:
        """Spec 2.9 retention: delete candidate reports and authenticity reports older than
        privacy.retain_raw_days (they hold candidate names and resume-derived profiles).
        Screening rows hold no personal data and are kept. The audit log is append-only and
        is never purged; the purge itself is recorded there."""
        days = float(cfg("privacy.retain_raw_days", 30) if days is None else days)
        cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)).isoformat()
        conn = self._conn()
        try:
            with conn:
                cand = conn.execute("DELETE FROM candidates WHERE created_at < ?", (cutoff,)).rowcount
                auth = conn.execute(
                    "DELETE FROM authenticity_reports WHERE created_at < ?", (cutoff,)
                ).rowcount
        finally:
            conn.close()
        result = {"candidates": cand, "authenticity_reports": auth, "retain_days": days}
        if cand or auth:
            self.append_audit("*", "retention_purge", result)
        return result

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
    def create_screening(
        self, sid: str, total: int, status: str = "processing", owner: str | None = None
    ) -> None:
        self._run(
            "INSERT INTO screenings (id, status, total, done, created_at, owner)"
            " VALUES (?,?,?,0,?,?)",
            (sid, status, total, _now(), owner),
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
    def save_candidate(
        self, cid: str, sid: str, report: dict, row: dict, owner: str | None = None
    ) -> None:
        self._run(
            "INSERT OR REPLACE INTO candidates"
            " (id, screening_id, report_json, row_json, created_at, owner) VALUES (?,?,?,?,?,?)",
            (cid, sid, json.dumps(report), json.dumps(row), _now(), owner),
        )

    def get_candidate(self, cid: str) -> dict | None:
        rows = self._run("SELECT report_json FROM candidates WHERE id=?", (cid,))
        return json.loads(rows[0]["report_json"]) if rows else None

    # standalone authenticity assessments (POST /v1/authenticity/assess)
    def put_authenticity_report(
        self, candidate_id: str, report: dict, owner: str | None = None
    ) -> None:
        self._run(
            "INSERT OR REPLACE INTO authenticity_reports"
            " (candidate_id, report_json, created_at, owner) VALUES (?,?,?,?)",
            (candidate_id, json.dumps(report), _now(), owner),
        )

    def get_authenticity_report(self, candidate_id: str) -> dict | None:
        rows = self._run(
            "SELECT report_json FROM authenticity_reports WHERE candidate_id=?", (candidate_id,)
        )
        return json.loads(rows[0]["report_json"]) if rows else None

    # ownership (A-30)
    def owner_of(self, table: str, key: str) -> tuple[bool, str | None]:
        """(row exists, its owner). The owner is None for rows written before ownership existed."""
        if table not in _OWNER_KEYS:
            raise ValueError(table)
        rows = self._run(f"SELECT owner FROM {table} WHERE {_OWNER_KEYS[table]}=?", (key,))
        return (True, rows[0]["owner"]) if rows else (False, None)

    def authenticity_owner(self, cid: str) -> tuple[bool, str | None]:
        """Owner of the authenticity report GET /v1/authenticity/{id} would serve: the
        standalone report first, else the screened candidate it is embedded in."""
        found, owner = self.owner_of("authenticity_reports", cid)
        return (found, owner) if found else self.owner_of("candidates", cid)

    def was_erased(self, cid: str) -> bool:
        return bool(self._run(
            "SELECT 1 FROM audit_log WHERE candidate_id=? AND event='erased' LIMIT 1", (cid,)
        ))

    # recruiter decisions: the free-text note lives in `notes` (erasable), the append-only
    # audit entry keeps only a note_present flag. Both rows commit in one transaction.
    def record_decision(self, cid: str, payload: dict[str, Any], note: str) -> None:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO audit_log (candidate_id, event, payload_json, at) VALUES (?,?,?,?)",
                    (cid, "recruiter_decision",
                     json.dumps({**payload, "note_present": bool(note)}), _now()),
                )
                if note:
                    conn.execute(
                        "INSERT INTO notes (audit_id, candidate_id, note) VALUES (?,?,?)",
                        (cur.lastrowid, cid, note),
                    )
        finally:
            conn.close()

    def decisions(self, cid: str) -> list[dict]:
        """Recruiter decisions in the original response shape (`note` is a string again).
        Legacy entries written before notes moved keep their note inside the payload."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT a.payload_json, n.note FROM audit_log a"
                " LEFT JOIN notes n ON n.audit_id = a.id"
                " WHERE a.candidate_id=? AND a.event='recruiter_decision' ORDER BY a.id",
                (cid,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            p = json.loads(r["payload_json"])
            has_flag = "note_present" in p
            p.pop("note_present", None)
            if has_flag:
                p["note"] = r["note"] or ""
            out.append(p)
        return out

    # erasure (right to be forgotten). Never touches audit_log except to append `erased`.
    def _erase(self, conn: sqlite3.Connection, cids: list[str]) -> dict:
        counts = {"candidates": 0, "authenticity_reports": 0, "notes": 0,
                  "audit_notes_retained": 0}
        for cid in cids:
            counts["candidates"] += conn.execute("DELETE FROM candidates WHERE id=?", (cid,)).rowcount
            counts["authenticity_reports"] += conn.execute(
                "DELETE FROM authenticity_reports WHERE candidate_id=?", (cid,)
            ).rowcount
            counts["notes"] += conn.execute("DELETE FROM notes WHERE candidate_id=?", (cid,)).rowcount
            for r in conn.execute(
                "SELECT payload_json FROM audit_log WHERE candidate_id=? AND event='recruiter_decision'",
                (cid,),
            ):
                p = json.loads(r["payload_json"])
                if "note_present" not in p and p.get("note"):
                    counts["audit_notes_retained"] += 1  # legacy: text already in the log
        return counts

    @staticmethod
    def _erased_event(
        conn: sqlite3.Connection, key: str, payload: dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log (candidate_id, event, payload_json, at) VALUES (?,?,?,?)",
            (key, "erased", json.dumps(payload), _now()),
        )

    def erase_candidate(self, cid: str, actor: str | None = None) -> dict | None:
        """Delete a candidate's report, row, authenticity report and notes. None when there was
        nothing to delete (the caller answers 404). Audit gets ids, counts and the label only."""
        conn = self._conn()
        try:
            with conn:
                counts = self._erase(conn, [cid])
                if not (counts["candidates"] or counts["authenticity_reports"]):
                    return None
                self._erased_event(
                    conn, cid, {"scope": "candidate", "counts": counts,
                                **({"actor": actor} if actor else {})},
                )
        finally:
            conn.close()
        return counts

    def erase_screening(self, sid: str, actor: str | None = None) -> dict | None:
        """Delete a screening and every candidate in it. One `erased` event per candidate (so
        each id reads as erased) plus one for the screening."""
        conn = self._conn()
        try:
            with conn:
                if not conn.execute("SELECT 1 FROM screenings WHERE id=?", (sid,)).fetchall():
                    return None
                cids = [r["id"] for r in conn.execute(
                    "SELECT id FROM candidates WHERE screening_id=?", (sid,))]
                counts = self._erase(conn, cids)
                for cid in cids:
                    self._erased_event(
                        conn, cid, {"scope": "candidate", "screening_id": sid,
                                    **({"actor": actor} if actor else {})},
                    )
                counts["screenings"] = conn.execute(
                    "DELETE FROM screenings WHERE id=?", (sid,)).rowcount
                self._erased_event(
                    conn, sid, {"scope": "screening", "counts": counts,
                                **({"actor": actor} if actor else {})},
                )
        finally:
            conn.close()
        return counts

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
