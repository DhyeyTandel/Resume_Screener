"""SQLite persistence: store round-trips, append-only audit, API read-back."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import store as store_mod
from app.db.store import Store
from app.main import app

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
JD = (SAMPLES / "jd_backend_engineer.txt").read_text()
STRONG = (SAMPLES / "resumes/01_strong_match.txt").read_text()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "sub" / "t.db"
    monkeypatch.setenv("SCREENING_DB_PATH", str(p))
    return p


def test_round_trip_screening_and_candidate(db_path):
    s = Store(db_path)
    s.create_screening("s1", 1)
    report = {"candidate_name": "A", "extensions": {"candidate_id": "c1"}}
    row = {"candidate_id": "c1", "candidate_name": "A"}
    s.save_candidate("c1", "s1", report, row)
    s.update_screening("s1", status="complete", done=1)
    assert s.get_screening("s1") == {
        "screening_id": "s1", "status": "complete", "total": 1, "done": 1,
        "candidates": [row],
    }
    assert s.get_candidate("c1") == report
    assert s.get_screening("nope") is None
    assert s.get_candidate("nope") is None


def test_audit_log_is_append_only(db_path):
    s = Store(db_path)
    s.append_audit("c1", "recruiter_decision", {"decision": "advance"})
    s.append_audit("c1", "screening_completed", {"x": 1})
    s.append_audit("c2", "recruiter_decision", {"decision": "reject"})
    log = s.list_audit("c1")
    assert [e["event"] for e in log] == ["recruiter_decision", "screening_completed"]
    assert [e["id"] for e in log] == sorted(e["id"] for e in log)
    public = [n for n in dir(Store) if not n.startswith("_") and "audit" in n]
    assert sorted(public) == ["append_audit", "list_audit"]
    for name in dir(Store):
        assert not (("audit" in name) and name.startswith(("update", "delete", "remove")))
    assert len(s.list_audit()) == 3


def test_persistence_across_fresh_store_instance(db_path):
    a = Store(db_path)
    a.create_screening("s1", 1)
    a.save_candidate("c1", "s1", {"k": "v"}, {"candidate_id": "c1"})
    a.append_audit("c1", "recruiter_decision", {"decision": "advance"})
    b = Store(db_path)
    assert b.get_candidate("c1") == {"k": "v"}
    assert b.get_screening("s1")["candidates"] == [{"candidate_id": "c1"}]
    assert b.list_audit("c1")[0]["payload"] == {"decision": "advance"}


def test_default_path_uses_env_override(db_path):
    assert store_mod.default_db_path() == db_path
    store_mod.get_store()
    assert db_path.exists()


def test_api_post_then_read_back_and_audit(db_path):
    with TestClient(app) as client:
        r = client.post("/v1/screenings", data={"jd_text": JD, "pasted_resumes": [STRONG]})
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"screening_id", "total_candidates", "status"}
        sid = body["screening_id"]
        for _ in range(100):
            sc = client.get(f"/v1/screenings/{sid}").json()
            if sc["status"] == "complete":
                break
            time.sleep(0.1)
        assert set(sc) == {"screening_id", "status", "total", "done", "candidates"}
        assert sc["status"] == "complete" and sc["done"] == 1
        cid = sc["candidates"][0]["candidate_id"]
        rep = client.get(f"/v1/candidates/{cid}").json()
        assert rep["extensions"]["candidate_id"] == cid

        d = client.post(f"/v1/candidates/{cid}/decision", data={"decision": "advance", "note": "ok"})
        assert set(d.json()) == {"candidate_id", "decision", "note", "at", "config_version"}
        audit = client.get(f"/v1/candidates/{cid}/audit").json()
        assert [a["decision"] for a in audit] == ["advance"]

        events = [e["event"] for e in Store(db_path).list_audit(cid)]
        assert events == ["screening_completed", "recruiter_decision"]
        done = Store(db_path).list_audit(cid, event="screening_completed")[0]["payload"]
        assert {"integrity_verdict", "integrity_action", "config_version"} <= set(done)

        assert client.get("/v1/screenings/zzz").status_code == 404
        assert client.get("/v1/candidates/zzz").status_code == 404
    # restart: a brand new client still sees the data
    with TestClient(app) as client2:
        assert client2.get(f"/v1/screenings/{sid}").json()["status"] == "complete"


def test_audit_log_rejects_update_and_delete_at_db_level(tmp_path):
    import sqlite3

    import pytest

    from app.db.store import Store

    s = Store(tmp_path / "a.db")
    s.append_audit("c1", "recruiter_decision", {"decision": "Hold"})
    conn = sqlite3.connect(tmp_path / "a.db")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET event='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    conn.close()
    assert len(s.list_audit("c1")) == 1


def test_screening_cut_off_by_restart_is_marked_interrupted(tmp_path):
    from app.db.store import Store

    Store(tmp_path / "b.db").create_screening("s1", total=2)
    reopened = Store(tmp_path / "b.db")  # simulates a server restart
    assert reopened.get_screening("s1")["status"] == "interrupted"
