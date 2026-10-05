"""Opt-in API key auth, CORS allowlist and interview-questions validation (A-28)."""
import hashlib

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.db.store import get_store
from app.main import app

KEY = "test-key-alpha-123"
OTHER = "test-key-beta-456"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENING_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("SCREENING_API_KEYS", raising=False)


@pytest.fixture
def client(db):
    return TestClient(app)


@pytest.fixture
def locked(db, monkeypatch):
    monkeypatch.setenv("SCREENING_API_KEYS", f"alice:{KEY}, bob:{OTHER}")
    return TestClient(app)


def test_open_mode_unchanged(client):
    h = client.get("/v1/health").json()
    assert h["auth_required"] is False
    assert client.get("/v1/samples").status_code == 200
    assert client.get("/v1/candidates/nope").status_code == 404


def test_blank_env_is_open(db, monkeypatch):
    monkeypatch.setenv("SCREENING_API_KEYS", "   ")
    assert TestClient(app).get("/v1/samples").status_code == 200


def test_missing_key_is_401_with_standard_body(locked):
    r = locked.get("/v1/samples")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    body = r.json()
    assert body["code"] == "UNAUTHORIZED" and body["message"] and body["remediation"]


def test_wrong_key_rejected(locked):
    assert locked.get("/v1/samples", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert locked.get("/v1/samples", headers={"X-API-Key": "nope"}).status_code == 401
    assert locked.get("/v1/samples", headers={"Authorization": f"Basic {KEY}"}).status_code == 401


def test_bearer_and_x_api_key_accepted(locked):
    assert locked.get("/v1/samples", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
    assert locked.get("/v1/samples", headers={"authorization": f"bearer {OTHER}"}).status_code == 200
    assert locked.get("/v1/samples", headers={"X-API-Key": KEY}).status_code == 200


def test_key_in_query_string_rejected(locked):
    for name in ("api_key", "key", "access_token", "token"):
        assert locked.get(f"/v1/samples?{name}={KEY}").status_code == 401


def test_health_public_and_reveals_nothing(locked):
    r = locked.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["auth_required"] is True
    text = r.text
    for secret in (KEY, OTHER, "alice", "bob"):
        assert secret not in text


def test_every_other_v1_route_requires_key(locked):
    for method, path in [
        ("get", "/v1/samples"), ("get", "/v1/samples/jd"), ("get", "/v1/screenings/x"),
        ("get", "/v1/candidates/x"), ("get", "/v1/candidates/x/audit"),
        ("post", "/v1/candidates/x/decision"), ("post", "/v1/screenings"),
        ("post", "/v1/interview-questions"), ("post", "/v1/skills/analyze"),
        ("post", "/v1/authenticity/assess"), ("get", "/v1/authenticity/x"),
        ("get", "/v1/nonexistent"),
    ]:
        assert getattr(locked, method)(path).status_code == 401, path


def test_static_frontend_stays_public(locked):
    assert locked.get("/").status_code == 200


def test_sha256_entries(db, monkeypatch):
    h = hashlib.sha256(KEY.encode()).hexdigest()
    monkeypatch.setenv("SCREENING_API_KEYS", f"carol:sha256:{h},sha256:{hashlib.sha256(OTHER.encode()).hexdigest()}")
    c = TestClient(app)
    assert c.get("/v1/samples", headers={"X-API-Key": KEY}).status_code == 200
    assert c.get("/v1/samples", headers={"X-API-Key": OTHER}).status_code == 200
    assert c.get("/v1/samples", headers={"X-API-Key": h}).status_code == 401  # the hash is not a key


def test_malformed_config_fails_closed(db, monkeypatch):
    monkeypatch.setenv("SCREENING_API_KEYS", "no-colon-here")
    c = TestClient(app)
    assert c.get("/v1/health").json()["auth_required"] is True
    assert c.get("/v1/samples", headers={"X-API-Key": "no-colon-here"}).status_code == 401


def _seed(cid="c1"):
    get_store().save_candidate(
        cid, "s1", {"candidate_name": "X", "extensions": {"candidate_id": cid}},
        {"candidate_id": cid, "candidate_name": "X", "overall_match_score": 1,
         "recommendation": "Review Manually", "status": "Complete"},
    )


def test_decision_audit_carries_label_never_key(locked):
    _seed()
    r = locked.post(
        "/v1/candidates/c1/decision", data={"decision": "advance", "note": "ok"},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert r.status_code == 200 and r.json()["actor"] == "alice"
    rows = get_store().list_audit("c1", event="recruiter_decision")
    assert rows[-1]["payload"]["actor"] == "alice"
    dump = repr(get_store().list_audit()) + r.text
    assert KEY not in dump and hashlib.sha256(KEY.encode()).hexdigest() not in dump
    audit = locked.get("/v1/candidates/c1/audit", headers={"X-API-Key": OTHER}).json()
    assert audit[0]["actor"] == "alice"


def test_open_mode_decision_has_no_actor(client):
    _seed()
    r = client.post("/v1/candidates/c1/decision", data={"decision": "advance"})
    assert "actor" not in r.json()
    assert "actor" not in get_store().list_audit("c1", event="recruiter_decision")[-1]["payload"]


def test_cors_default_allowlist(client):
    ok = client.get("/v1/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    bad = client.get("/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_cors_preflight_allows_authorization_header(client):
    r = client.options(
        "/v1/samples",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
                 "Access-Control-Request-Headers": "authorization,x-api-key"},
    )
    assert r.status_code == 200
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed and "x-api-key" in allowed
    evil = client.options(
        "/v1/samples",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert evil.status_code == 400


def test_401_carries_cors_headers(locked, monkeypatch):
    r = locked.get("/v1/samples", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_origins_env(monkeypatch):
    monkeypatch.setenv("SCREENING_CORS_ORIGINS", "https://a.example/, https://b.example")
    assert main_mod.cors_origins() == ["https://a.example", "https://b.example"]
    monkeypatch.setenv("SCREENING_CORS_ORIGINS", "")
    assert main_mod.cors_origins() == main_mod.DEFAULT_CORS_ORIGINS


def test_interview_questions_valid(client):
    r = client.post("/v1/interview-questions", json={"requirements": [
        {"skill": "Go", "evidence_level": "strong_evidence", "jd_priority": "must_have"}]})
    assert r.status_code == 200
    assert r.json()["skipped_strong_evidence"] == ["Go"]


@pytest.mark.parametrize("body,field", [
    ({"requirements": [{"skill": "Go", "evidence_level": "bogus"}]}, "requirements.0.evidence_level"),
    ({"requirements": [{"evidence_level": "claimed"}]}, "requirements.0.skill"),
    ({"requirements": [{"skill": "", "evidence_level": "claimed"}]}, "requirements.0.skill"),
    ({"requirements": [{"skill": "x" * 201, "evidence_level": "claimed"}]}, "requirements.0.skill"),
    ({"requirements": [{"skill": "a", "evidence_level": "claimed", "jd_priority": "urgent"}]},
     "requirements.0.jd_priority"),
    ({"requirements": [{"skill": "a", "evidence_level": "claimed"}] * 101}, "requirements"),
    ({"requirements": "nope"}, "requirements"),
])
def test_interview_questions_422_standard_body(client, body, field):
    r = client.post("/v1/interview-questions", json=body)
    assert r.status_code == 422
    j = r.json()
    assert j["code"] == "INVALID_REQUEST" and j["message"] and j["remediation"]
    assert j["field"] == field
