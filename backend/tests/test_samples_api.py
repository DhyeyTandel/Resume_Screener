"""Tests for the sample-data endpoints that feed the landing dashboard."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_samples_returns_full_reports_sorted_by_filename():
    res = client.get("/v1/samples")
    assert res.status_code == 200
    reports = res.json()
    assert len(reports) >= 1
    for r in reports:
        assert r["candidate_name"]
        assert "overall_match_score" in r
        assert r["extensions"]["candidate_id"]
        assert "requirement_match" in r
    # sorted by filename: 01_strong_match first
    assert reports[0]["candidate_name"] == "Priya Raman"
    ids = [r["extensions"]["candidate_id"] for r in reports]
    assert len(ids) == len(set(ids))


def test_sample_jd_is_plain_text():
    res = client.get("/v1/samples/jd")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "Backend Engineer" in res.text
    assert "Python" in res.text


def test_samples_path_does_not_shadow_other_routes():
    assert client.get("/v1/health").status_code == 200
