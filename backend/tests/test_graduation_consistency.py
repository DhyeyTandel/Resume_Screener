import re
from pathlib import Path

from app.llm.client import LLMClient
from app.modules.authenticity_engine.consistency import check_graduation_consistency
from app.pipeline.orchestrator import screen_candidate

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
JD = (SAMPLES / "jd_backend_engineer.txt").read_text()
EDU = ["B.S. Computer Science, Example University, 2019"]


def role(title, start, end="2021"):
    return {"title": title, "company": "Acme", "start": str(start), "end": end}


def test_no_education_year_returns_nothing():
    assert check_graduation_consistency(["B.S. Computer Science"], [role("Senior Engineer", 2015)]) == []
    assert check_graduation_consistency([], [role("Senior Engineer", 2015)]) == []


def test_senior_role_well_before_graduation_is_flagged():
    out = check_graduation_consistency(EDU, [role("Senior Engineer", 2015)])
    assert len(out) == 1 and out[0]["type"] == "graduation_inconsistency"
    assert out[0]["sources"] == ["resume"]
    words = set(re.findall(r"[a-z]+", out[0]["detail"].lower()))
    assert not words & {"fake", "fraud", "lie", "lied"}


def test_intern_is_not_flagged():
    assert check_graduation_consistency(EDU, [role("Software Engineer Intern", 2017, "2018")]) == []


def test_ordinary_role_starting_before_graduation_is_not_flagged():
    assert check_graduation_consistency(EDU, [role("Software Engineer", 2018)]) == []


def test_lead_starting_in_graduation_year_is_not_flagged():
    assert check_graduation_consistency(EDU, [role("Lead Engineer", 2019)]) == []


async def test_finding_reaches_authenticity_contradictions():
    text = (
        "Jane Doe\njane@example.com\n\nSkills\nPython, SQL\n\nExperience\n"
        "Senior Engineer at Acme, 2015 - 2021\n- Built Python services\n\n"
        "Education\nB.S. Computer Science, Example University, 2019\n"
    )
    r = await screen_candidate(jd_text=JD, pasted_text=text, llm=LLMClient("mock"))
    cs = r["extensions"]["authenticity"]["contradictions"]
    assert any(c["type"] == "graduation_inconsistency" for c in cs)
