"""Module B Stage 4: deterministic anachronism detection."""
from app.modules.authenticity_engine.consistency import check_anachronisms, release_years


def test_release_years_load():
    years = release_years()
    assert years["fastapi"] == 2018
    assert years["postgresql"] == 1996


def test_anachronism_detected():
    text = "10 years of FastAPI experience building production APIs."
    out = check_anachronisms(text)
    assert len(out) == 1
    assert out[0]["type"] == "technology_anachronism"
    assert "FastAPI" in out[0]["detail"]


def test_plausible_years_not_flagged():
    text = "3 years of FastAPI experience and 8 years of PostgreSQL."
    out = check_anachronisms(text)
    assert out == []


def test_unknown_technology_not_flagged():
    text = "20 years of Excel and 15 years of leadership."
    out = check_anachronisms(text)
    assert out == []
