"""Module-level endpoints: authenticity assess/get and skills analyze (Spec 14.1)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
TRANSFERABLE = (SAMPLES / "resumes/02_transferable.txt").read_text()
JD = (SAMPLES / "jd_backend_engineer.txt").read_text()
NO_CONSENT = {"github": False, "linkedin": False, "portfolio": False}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENING_DB_PATH", str(tmp_path / "t.db"))
    return TestClient(app)


def _assess(client, cid="c-1", text=TRANSFERABLE):
    return client.post(
        "/v1/authenticity/assess",
        json={
            "candidate_id": cid,
            "resume": {"raw_text": text},
            "job_context": {"required_skills": ["Python", "FastAPI"], "role_level": "mid"},
            "consent": NO_CONSENT,
        },
    )


def test_assess_all_no_consent(client):
    r = _assess(client)
    assert r.status_code == 200
    body = r.json()
    assert body["candidate_id"] == "c-1"
    assert body["sources_used"] == {
        "github": "no_consent", "linkedin": "no_consent", "portfolio": "no_consent"
    }
    assert body["band"] == "INSUFFICIENT_EVIDENCE"


def test_get_after_assess_matches(client):
    posted = _assess(client).json()
    got = client.get("/v1/authenticity/c-1")
    assert got.status_code == 200
    assert got.json() == posted


def test_get_unknown_is_404_with_error_body(client):
    r = client.get("/v1/authenticity/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"
    assert "message" in r.json()["detail"]


def test_get_falls_back_to_screened_candidate(client):
    from app.db.store import get_store

    block = {"band": "MODERATE", "candidate_id": "s-1"}
    get_store().save_candidate(
        "s-1", "scr", {"extensions": {"authenticity": block}}, {"candidate_id": "s-1"}
    )
    assert client.get("/v1/authenticity/s-1").json() == block


def test_skills_analyze_required_skills(client):
    r = client.post(
        "/v1/skills/analyze", json={"resume_text": TRANSFERABLE, "required_skills": ["FastAPI"]}
    )
    assert r.status_code == 200
    out = r.json()
    assert len(out) == 1
    assert out[0]["required_skill"] == "FastAPI"
    assert out[0]["classification"] == "Strongly Transferable"


def test_skills_analyze_with_jd_text(client):
    r = client.post("/v1/skills/analyze", json={"resume_text": TRANSFERABLE, "jd_text": JD})
    assert r.status_code == 200
    out = r.json()
    assert out and all("classification" in o and "required_skill" in o for o in out)


@pytest.mark.parametrize("payload", [{}, {"resume_text": ""}, {"resume_text": "   "}])
def test_empty_resume_text_is_422(client, payload):
    r = client.post("/v1/skills/analyze", json={**payload, "required_skills": ["FastAPI"]})
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "RESUME_REQUIRED" and body["field"] == "resume_text"
    assert body["remediation"]


def test_assess_empty_resume_is_422(client):
    r = client.post("/v1/authenticity/assess", json={"resume": {"raw_text": ""}})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RESUME_REQUIRED"


def test_schema_error_uses_standard_body(client):
    r = client.post("/v1/skills/analyze", json={"resume_text": 5, "required_skills": "x"})
    assert r.status_code == 422
    body = r.json()
    assert {"code", "message", "field", "remediation"} <= set(body)


def test_oversize_text_rejected(client, monkeypatch):
    import app.api.routes as routes

    monkeypatch.setattr(routes, "cfg", lambda k, d=None: 10 if k == "ingest.max_bytes" else d)
    r = client.post(
        "/v1/skills/analyze", json={"resume_text": "x" * 50, "required_skills": ["FastAPI"]}
    )
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "TEXT_TOO_LARGE"


def test_pii_not_in_outputs(client):
    text = "Zelda Quimby\nzelda.quimby@example.com\n\n" + TRANSFERABLE.split("\n", 2)[2]
    skills = client.post(
        "/v1/skills/analyze", json={"resume_text": text, "required_skills": ["FastAPI"]}
    ).text
    auth = _assess(client, text=text).text
    for blob in (skills, auth):
        assert "zelda.quimby@example.com" not in blob.lower()
        assert "Zelda" not in blob and "Quimby" not in blob
