"""Environment overrides that the docs promise must actually take effect."""
from app import config


def test_ollama_base_url_and_model_env_overrides(monkeypatch):
    # Tests the override function on a fresh dict. Clearing get_config's cache here once
    # reloaded config.yaml mid-suite and re-enabled the rate limiter conftest switches off,
    # so every later screening test got 429s.
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    out = config._apply_env_overrides({"llm": {"provider": "mock", "ollama": {"base_url": "x", "model": "y"}}})
    assert out["llm"]["ollama"] == {"base_url": "http://ollama:11434", "model": "llama3.1:8b"}


def test_empty_jd_is_rejected_with_a_field_level_error(tmp_path, monkeypatch):
    """Spec 7 / 17: missing or empty JD gives 422 with a field-level message (the UI test
    alone did not cover the backend)."""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("SCREENING_DB_PATH", str(tmp_path / "jd.db"))
    with TestClient(app) as client:
        for jd in ("", "   "):
            r = client.post("/v1/screenings", data={"jd_text": jd, "pasted_resumes": ["Jane\nPython"]})
            assert r.status_code == 422, (jd, r.status_code)
            body = r.json()
            body = body.get("detail", body) if isinstance(body.get("detail"), dict) else body
            assert body.get("field") == "jd_text" or "jd_text" in str(r.json()), r.json()
