"""Protected-attribute redaction (Spec 2.2)."""
from app.llm.redaction import has_protected_attributes, redact_for_scoring

RESUME = """Priya Raman
priya.raman@example.com | +1 555 0100 | https://priya.dev
Date of Birth: 12 March 1996
Gender: Female
Nationality: Indian
Experience
Senior Backend Engineer, built FastAPI services
Education
B.S. in Computer Science, Ravenna University, 2019
"""


def test_name_contact_and_protected_lines_are_removed():
    out = redact_for_scoring(RESUME, candidate_name="Priya Raman")
    for leaked in ["Priya", "Raman", "priya.raman@example.com", "555 0100",
                   "Date of Birth", "Gender", "Nationality", "Female", "1996"]:
        assert leaked not in out, f"{leaked} survived redaction"
    assert "[CANDIDATE]" in out and "[CONTACT]" in out


def test_institution_masked_but_degree_level_and_field_kept():
    out = redact_for_scoring(RESUME, candidate_name="Priya Raman")
    assert "Ravenna University" not in out
    assert "[INSTITUTION]" in out
    assert "B.S. in Computer Science" in out


def test_job_relevant_content_survives():
    out = redact_for_scoring(RESUME, candidate_name="Priya Raman")
    assert "FastAPI" in out and "Senior Backend Engineer" in out


def test_detector():
    assert has_protected_attributes(RESUME)
    assert not has_protected_attributes("Built FastAPI services and tuned PostgreSQL queries.")
