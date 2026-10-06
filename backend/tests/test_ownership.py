"""Per-recruiter ownership and erasure (A-30). Ownership is active only when auth is on."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.db.store import Store, get_store
from app.main import app

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
JD = (SAMPLES / "jd_backend_engineer.txt").read_text()
STRONG = (SAMPLES / "resumes/01_strong_match.txt").read_text()

A, B, ADM = "key-alice-123", "key-bob-456", "key-root-789"
HA, HB, HADM = ({"Authorization": f"Bearer {k}"} for k in (A, B, ADM))
PII = "Zebediah Quux"
NOTE = "Call his mother Gertrude on 555-0100"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENING_DB_PATH", str(tmp_path / "o.db"))
    monkeypatch.delenv("SCREENING_API_KEYS", raising=False)
    monkeypatch.delenv("SCREENING_ADMIN_LABELS", raising=False)
    return tmp_path / "o.db"


@pytest.fixture
def open_client(db):
    return TestClient(app)


@pytest.fixture
def locked(db, monkeypatch):
    monkeypatch.setenv("SCREENING_API_KEYS", f"alice:{A},bob:{B},root:{ADM}")
    monkeypatch.setenv("SCREENING_ADMIN_LABELS", "root")
    return TestClient(app)


def _seed(owner="alice", sid="s1", cid="c1", authid="a1"):
    s = get_store()
    s.create_screening(sid, 1, status="complete", owner=owner)
    report = {"candidate_name": PII,
              "extensions": {"candidate_id": cid, "authenticity": {"band": "LOW"}}}
    s.save_candidate(cid, sid, report, {"candidate_id": cid, "candidate_name": PII}, owner=owner)
    s.put_authenticity_report(authid, {"candidate_id": authid, "name": PII}, owner=owner)
    return s


def _decide(client, cid="c1", headers=None, note=NOTE):
    return client.post(
        f"/v1/candidates/{cid}/decision", data={"decision": "advance", "note": note},
        headers=headers or {},
    )


# (method, path template, id placeholder value for the owner's seed, id for a missing row)
ROUTES = [
    ("GET", "/v1/screenings/{}", "s1", "nope"),
    ("GET", "/v1/candidates/{}", "c1", "nope"),
    ("POST", "/v1/candidates/{}/decision", "c1", "nope"),
    ("GET", "/v1/candidates/{}/audit", "c1", "nope"),
    ("GET", "/v1/authenticity/{}", "a1", "nope"),
    ("GET", "/v1/authenticity/{}", "c1", "nope"),  # falls back to the screened candidate
    ("DELETE", "/v1/candidates/{}", "c1", "nope"),
    ("DELETE", "/v1/screenings/{}", "s1", "nope"),
]


def _call(client, method, path, headers, ident):
    kw = {"data": {"decision": "hold"}} if method == "POST" else {}
    return client.request(method, path.format(ident), headers=headers, **kw)


@pytest.mark.parametrize("method,path,ident,missing", ROUTES)
def test_matrix_owner_other_and_admin(locked, method, path, ident, missing):
    # Each principal gets its own seed (ids suffixed), because the DELETE rows consume it.
    for n, (headers, ok) in enumerate(((HA, True), (HADM, True), (HB, False))):
        t = str(n)
        _seed(sid="s1" + t, cid="c1" + t, authid="a1" + t)
        target = ident + t
        r = _call(locked, method, path, headers, target)
        if ok:
            assert r.status_code == 200, (headers, r.text)
            continue
        assert r.status_code == 404
        gone = _call(locked, method, path, headers, missing)
        assert gone.status_code == 404
        # Same body as a truly missing id, modulo the echoed id.
        rb, gb = r.json()["detail"], gone.json()["detail"]
        assert rb["code"] == gb["code"] == "NOT_FOUND"
        assert rb["message"].replace(target, "X") == gb["message"].replace(missing, "X")
        assert set(rb) == set(gb)
        # And the refused call changed nothing: the owner still sees it.
        assert locked.get("/v1/candidates/c1" + t, headers=HA).status_code == 200
        assert locked.get("/v1/screenings/s1" + t, headers=HA).status_code == 200


def test_rows_without_owner_are_admin_only(locked):
    _seed(owner=None)
    for path in ("/v1/screenings/s1", "/v1/candidates/c1", "/v1/candidates/c1/audit",
                 "/v1/authenticity/a1"):
        assert locked.get(path, headers=HA).status_code == 404, path
        assert locked.get(path, headers=HADM).status_code == 200, path
    assert _decide(locked, headers=HA).status_code == 404
    assert _decide(locked, headers=HADM).status_code == 200


def test_decision_on_unseen_candidate_is_404_and_writes_nothing(locked):
    _seed()
    assert _decide(locked, headers=HB).status_code == 404
    assert get_store().list_audit("c1", event="recruiter_decision") == []
    assert _decide(locked, headers=HA).status_code == 200


def test_samples_readable_by_any_key(locked):
    for h in (HA, HB, HADM):
        assert locked.get("/v1/samples", headers=h).status_code == 200
        assert locked.get("/v1/samples/jd", headers=h).status_code == 200


def test_open_mode_ignores_ownership(open_client):
    _seed(owner="alice")
    assert open_client.get("/v1/candidates/c1").status_code == 200
    _seed(owner=None, sid="s2", cid="c2", authid="a2")
    assert open_client.get("/v1/candidates/c2/audit").json() == []


def test_screening_records_owner_end_to_end(locked):
    r = locked.post("/v1/screenings", data={"jd_text": JD, "pasted_resumes": [STRONG]}, headers=HA)
    sid = r.json()["screening_id"]
    for _ in range(100):
        sc = locked.get(f"/v1/screenings/{sid}", headers=HA).json()
        if sc["status"] == "complete":
            break
        time.sleep(0.1)
    cid = sc["candidates"][0]["candidate_id"]
    store = get_store()
    assert store.owner_of("screenings", sid) == (True, "alice")
    assert store.owner_of("candidates", cid) == (True, "alice")
    assert locked.get(f"/v1/screenings/{sid}", headers=HB).status_code == 404
    assert locked.get(f"/v1/candidates/{cid}", headers=HB).status_code == 404
    assert locked.get(f"/v1/candidates/{cid}", headers=HADM).status_code == 200


def test_assess_cannot_overwrite_another_recruiters_report(locked):
    _seed()
    body = {"candidate_id": "a1", "resume": {"raw_text": STRONG}}
    r = locked.post("/v1/authenticity/assess", json=body, headers=HB)
    assert r.status_code == 200
    assert store_owner("a1") == "alice"  # untouched
    new_id = r.json().get("candidate_id")
    assert new_id and new_id != "a1" and store_owner(new_id) == "bob"
    assert locked.get("/v1/authenticity/a1", headers=HB).status_code == 404


def store_owner(key):
    return get_store().owner_of("authenticity_reports", key)[1]


# --- the route-coverage guard -----------------------------------------------------------------
def _flatten(rs):
    for r in rs:
        inner = getattr(r, "original_router", None)  # newer FastAPI nests included routers
        if inner is not None:
            yield from _flatten(inner.routes)
        elif hasattr(r, "dependant"):
            yield r


def _v1_routes():
    found = [r for r in _flatten(app.routes) if r.path.startswith("/v1")]
    # Cross-check against what the app actually serves, so the walk cannot silently miss routes.
    served = {p for p in app.openapi()["paths"] if p.startswith("/v1")}
    assert served and served == {r.path for r in found}
    return found


def _dep_calls(route):
    out, stack = set(), list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        out.add(d.call)
        stack.extend(d.dependencies)
    return out


# Routes that take no id in the path. assess accepts an id in the body and handles it in-route
# (see test_assess_cannot_overwrite_another_recruiters_report). A new route must either be
# guarded or be added here on purpose.
NO_ID_ROUTES = {
    ("GET", "/v1/health"), ("GET", "/v1/samples"), ("GET", "/v1/samples/jd"),
    ("POST", "/v1/screenings"), ("POST", "/v1/interview-questions"),
    ("POST", "/v1/authenticity/assess"), ("POST", "/v1/skills/analyze"),
}


def test_every_id_route_has_an_ownership_guard():
    seen = set()
    for route in _v1_routes():
        for method in route.methods - {"HEAD", "OPTIONS"}:
            seen.add((method, route.path))
            if "{" in route.path:
                assert _dep_calls(route) & routes.OWNERSHIP_GUARDS, (
                    f"{method} {route.path} takes an id but has no ownership guard"
                )
            else:
                assert (method, route.path) in NO_ID_ROUTES, (
                    f"{method} {route.path} is new: guard it or list it in NO_ID_ROUTES"
                )
    assert seen >= {("DELETE", "/v1/candidates/{cid}"), ("DELETE", "/v1/screenings/{sid}"),
                    ("POST", "/v1/candidates/{cid}/decision"), ("GET", "/v1/candidates/{cid}/audit")}
    assert seen >= NO_ID_ROUTES  # the allowlist has no stale entries


# --- migration ----------------------------------------------------------------------------------
OLD_SCHEMA = """
CREATE TABLE screenings (id TEXT PRIMARY KEY, status TEXT NOT NULL, total INTEGER NOT NULL,
    done INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE candidates (id TEXT PRIMARY KEY, screening_id TEXT NOT NULL,
    report_json TEXT NOT NULL, row_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE authenticity_reports (candidate_id TEXT PRIMARY KEY, report_json TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL,
    event TEXT NOT NULL, payload_json TEXT NOT NULL, at TEXT NOT NULL);
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def _old_db(path):
    now = "2999-01-01T00:00:00+00:00"  # far future so retention never purges it
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO screenings VALUES ('s1','complete',1,1,?)", (now,))
        conn.execute("INSERT INTO candidates VALUES ('c1','s1',?,?,?)",
                     (json.dumps({"candidate_name": "Old"}), json.dumps({"candidate_id": "c1"}), now))
        conn.execute("INSERT INTO authenticity_reports VALUES ('a1',?,?)", (json.dumps({"k": 1}), now))
        conn.execute(
            "INSERT INTO audit_log (candidate_id,event,payload_json,at) VALUES (?,?,?,?)",
            ("c1", "recruiter_decision",
             json.dumps({"decision": "hold", "note": "legacy note", "at": now,
                         "config_version": "x"}), now),
        )
    conn.close()


def _cols(path, table):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_migration_of_a_db_built_with_the_old_schema(tmp_path):
    path = tmp_path / "old.db"
    _old_db(path)
    for t in ("screenings", "candidates", "authenticity_reports"):
        assert "owner" not in _cols(path, t)
    s = Store(path)
    for t in ("screenings", "candidates", "authenticity_reports"):
        assert _cols(path, t).count("owner") == 1
    Store(path)  # idempotent: reopening does not fail or duplicate the column
    for t in ("screenings", "candidates", "authenticity_reports"):
        assert _cols(path, t).count("owner") == 1
    # Old data is intact and has no owner; new writes get one.
    assert s.owner_of("candidates", "c1") == (True, None)
    assert s.get_candidate("c1") == {"candidate_name": "Old"}
    s.save_candidate("c2", "s1", {}, {"candidate_id": "c2"}, owner="alice")
    assert s.owner_of("candidates", "c2") == (True, "alice")
    # The legacy audit entry still reads in the old shape, note included.
    assert s.decisions("c1")[0]["note"] == "legacy note"


def test_old_db_through_the_api_with_auth(tmp_path, monkeypatch):
    path = tmp_path / "old2.db"
    _old_db(path)
    monkeypatch.setenv("SCREENING_DB_PATH", str(path))
    monkeypatch.setenv("SCREENING_API_KEYS", f"alice:{A},root:{ADM}")
    monkeypatch.setenv("SCREENING_ADMIN_LABELS", "root")
    c = TestClient(app)
    assert c.get("/v1/candidates/c1", headers=HA).status_code == 404
    assert c.get("/v1/candidates/c1", headers=HADM).status_code == 200
    assert c.get("/v1/candidates/c1/audit", headers=HADM).json()[0]["note"] == "legacy note"


# --- erasure --------------------------------------------------------------------------------------
def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("screenings", "candidates", "authenticity_reports", "notes", "audit_log")}
    finally:
        conn.close()


def test_candidate_erasure_removes_everything_but_audit(open_client, db):
    _seed(owner=None)
    _seed(owner=None, sid="s2", cid="c2", authid="c2")  # c2 has a report AND an auth report
    assert _decide(open_client).status_code == 200
    before = _tables(db)
    r = open_client.delete("/v1/candidates/c1")
    assert r.status_code == 200
    counts = r.json()["erased"]
    assert counts["candidates"] == 1 and counts["notes"] == 1
    after = _tables(db)
    assert after["candidates"] == before["candidates"] - 1
    assert after["notes"] == 0
    assert after["screenings"] == before["screenings"]  # no personal data in a screening row
    assert after["audit_log"] == before["audit_log"] + 1  # kept, plus the erased event
    assert open_client.get("/v1/candidates/c1").status_code == 404
    assert open_client.get("/v1/candidates/c1/audit").status_code == 404
    assert open_client.get("/v1/authenticity/c1").status_code == 404
    assert _decide(open_client).status_code == 404
    assert open_client.get("/v1/candidates/c2").status_code == 200  # others untouched
    # c2's standalone authenticity report goes with it
    assert open_client.delete("/v1/candidates/c2").json()["erased"]["authenticity_reports"] == 1


def test_authenticity_only_candidate_can_be_erased(open_client):
    get_store().put_authenticity_report("solo", {"x": 1})
    assert open_client.delete("/v1/candidates/solo").json()["erased"]["authenticity_reports"] == 1
    assert open_client.get("/v1/authenticity/solo").status_code == 404


def test_double_delete_is_a_clean_404(open_client):
    _seed(owner=None)
    assert open_client.delete("/v1/candidates/c1").status_code == 200
    again = open_client.delete("/v1/candidates/c1")
    assert again.status_code == 404 and again.json()["detail"]["code"] == "NOT_FOUND"
    assert open_client.delete("/v1/screenings/s1").status_code == 200
    assert open_client.delete("/v1/screenings/s1").status_code == 404
    assert open_client.get("/v1/screenings/s1").status_code == 404
    assert open_client.delete("/v1/candidates/never-existed").status_code == 404


def test_screening_erasure_deletes_its_candidates(open_client, db):
    s = _seed(owner=None)
    s.save_candidate("c9", "s1", {"candidate_name": "Nine"}, {"candidate_id": "c9"})
    r = open_client.delete("/v1/screenings/s1")
    assert r.status_code == 200
    assert r.json()["erased"]["candidates"] == 2 and r.json()["erased"]["screenings"] == 1
    t = _tables(db)
    assert t["screenings"] == 0 and t["candidates"] == 0
    assert open_client.get("/v1/candidates/c9").status_code == 404
    assert open_client.get("/v1/candidates/c9/audit").status_code == 404
    kinds = [(e["candidate_id"], e["payload"]["scope"])
             for e in s.list_audit(event="erased")]
    assert ("s1", "screening") in kinds and ("c1", "candidate") in kinds


def test_running_screening_cannot_be_erased(open_client):
    get_store().create_screening("live", 1, status="processing")
    r = open_client.delete("/v1/screenings/live")
    assert r.status_code == 409 and r.json()["code"] == "SCREENING_IN_PROGRESS"
    assert open_client.get("/v1/screenings/live").status_code == 200


def test_audit_triggers_still_reject_update_and_delete(open_client, db):
    _seed(owner=None)
    _decide(open_client)
    open_client.delete("/v1/candidates/c1")
    conn = sqlite3.connect(db)
    for sql in ("UPDATE audit_log SET event='x'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql)
    conn.close()
    # Reopening the store (which re-runs the schema and migration) keeps the triggers.
    Store(db)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    conn.close()


def test_erased_event_holds_no_personal_data(locked, db):
    _seed(authid="c1")
    _decide(locked, headers=HA)
    assert locked.delete("/v1/candidates/c1", headers=HA).status_code == 200
    ev = get_store().list_audit("c1", event="erased")
    assert len(ev) == 1
    p = ev[0]["payload"]
    assert p["scope"] == "candidate" and p["actor"] == "alice"
    assert set(p["counts"]) == {"candidates", "authenticity_reports", "notes", "audit_notes_retained"}
    dump = repr(get_store().list_audit()) + json.dumps(p)
    assert PII not in dump and "Gertrude" not in dump and "555-0100" not in dump
    # the raw file contains neither the name nor the note anywhere in a readable table
    conn = sqlite3.connect(db)
    for t in ("candidates", "authenticity_reports", "notes", "audit_log"):
        for row in conn.execute(f"SELECT * FROM {t}"):
            assert PII not in repr(row) and "Gertrude" not in repr(row)
    conn.close()


def test_open_mode_erased_event_has_no_actor(open_client):
    _seed(owner=None)
    open_client.delete("/v1/candidates/c1")
    assert "actor" not in get_store().list_audit("c1", event="erased")[0]["payload"]


def test_note_lives_outside_the_audit_log_and_audit_response_shape_is_unchanged(open_client, db):
    _seed(owner=None)
    d = _decide(open_client).json()
    assert set(d) == {"candidate_id", "decision", "note", "at", "config_version"}
    audit = open_client.get("/v1/candidates/c1/audit").json()
    assert set(audit[0]) == {"candidate_id", "decision", "note", "at", "config_version"}
    assert audit[0]["note"] == NOTE
    stored = get_store().list_audit("c1", event="recruiter_decision")[0]["payload"]
    assert "note" not in stored and stored["note_present"] is True
    # a decision without a note round-trips as an empty string
    open_client.post("/v1/candidates/c1/decision", data={"decision": "hold"})
    assert open_client.get("/v1/candidates/c1/audit").json()[1]["note"] == ""


def test_legacy_audit_note_is_counted_as_retained(tmp_path):
    path = tmp_path / "old3.db"
    _old_db(path)
    counts = Store(path).erase_candidate("c1")
    assert counts is not None and counts["audit_notes_retained"] == 1


def test_erasure_ownership(locked):
    _seed()
    assert locked.delete("/v1/candidates/c1", headers=HB).status_code == 404
    assert locked.delete("/v1/screenings/s1", headers=HB).status_code == 404
    assert locked.get("/v1/candidates/c1", headers=HA).status_code == 200
    assert locked.delete("/v1/candidates/c1", headers=HA).status_code == 200
    # after erasure nobody, admin included, can read the audit
    assert locked.get("/v1/candidates/c1/audit", headers=HADM).status_code == 404
