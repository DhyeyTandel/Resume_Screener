"""Regressions from the adversarial audit: no-heading resumes, filename-derived names,
redaction gaps, degree and duration math, and silently dropped JD requirements."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.llm.client import LLMClient
from app.llm.redaction import has_protected_attributes, masking_names, redact_for_scoring
from app.modules.core_screening.core import (
    degree_level,
    extract_requirements,
    match_requirements,
    structure_resume,
    total_years,
    unrecognised_jd_lines,
)
from app.pipeline.orchestrator import screen_candidate
from app.policy.recommendation import NOT_RECOMMENDED, REVIEW, decide
from app.policy.scoring import compose
from app.schemas.report import UnifiedReport
from app.schemas.vocab import MATCHED, MISSING, NOT_ENOUGH

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
JD = (SAMPLES / "jd_backend_engineer.txt").read_text()
STRONG = (SAMPLES / "resumes/01_strong_match.txt").read_text()
NO_HEADINGS = (
    "Jordan Lee\njordan@example.com\n"
    "I build backend services in Python and FastAPI with PostgreSQL."
)


async def run(text, jd=JD, **kw):
    return await screen_candidate(jd_text=jd, pasted_text=text, llm=LLMClient("mock"), **kw)


def r(name, priority, status):
    return {"requirement": name, "priority": priority, "status": status}


# --- 1. No headings must never read as a negative -------------------------------------

async def test_resume_without_headings_is_never_not_recommended():
    out = await run(NO_HEADINGS)
    assert out["recommendation"] == REVIEW
    # The skills named in prose are found, so something is assessable and nothing is Missing.
    status = {q["requirement"]: q["status"] for q in out["requirement_match"]}
    assert status["Python"] == status["FastAPI"] == status["PostgreSQL"] == MATCHED
    assert MISSING not in status.values()
    assert any("could not be judged" in x or "assessed" in x
               for x in out["extensions"]["recommendation_reasons"])


def test_nothing_assessable_gives_review_with_explicit_reason_not_not_recommended():
    reqs = [r("A", "Must Have", NOT_ENOUGH), r("B", "Preferred", NOT_ENOUGH)]
    scores = compose(reqs)
    assert scores["score_assessable"] is False
    assert scores["base_score"] == 0.0 and scores["score_confidence"] == 0.0
    rec, reasons = decide(overall_score=0, score_confidence=0.0, requirements=reqs)
    assert rec == REVIEW
    assert "Too little of the resume could be assessed" in reasons[0]


def test_low_confidence_low_score_is_review_not_not_recommended():
    reqs = [r("A", "Must Have", MISSING), r("B", "Must Have", NOT_ENOUGH),
            r("C", "Must Have", NOT_ENOUGH), r("D", "Must Have", NOT_ENOUGH)]
    rec, _ = decide(overall_score=0, score_confidence=0.25, requirements=reqs)
    assert rec == REVIEW


def test_confident_low_score_is_still_not_recommended():
    reqs = [r("A", "Must Have", MISSING), r("B", "Must Have", MISSING)]
    assert decide(overall_score=0, score_confidence=1.0, requirements=reqs)[0] == NOT_RECOMMENDED


async def test_nothing_assessable_report_flags_the_score_and_validates():
    out = await run("Jordan Lee\njordan@example.com\nI enjoy hiking and chess.")
    assert out["recommendation"] == REVIEW
    assert out["extensions"]["score_breakdown"]["score_assessable"] is False
    UnifiedReport.model_validate(out)


def test_labelled_skills_line_outside_a_skills_section_is_found():
    r_ = structure_resume("Jordan Lee\nSkills: Python, FastAPI\nTech stack: Docker | Kafka\n")
    assert {"python", "fastapi", "docker", "kafka"} <= {s.lower() for s in r_["skills"]}


def test_skills_in_prose_under_no_heading_are_found():
    r_ = structure_resume("Jordan Lee\nBackend developer who ships Python and PostgreSQL services.")
    assert {"python", "postgresql"} <= {s.lower() for s in r_["skills"]}


def test_education_prose_is_not_mined_for_skills():
    r_ = structure_resume("Education\nB.Tech, Python for Everybody course\n")
    assert r_["skills"] == []


# --- 2. Filename-derived names must not mask skills -----------------------------------

async def test_filename_derived_name_does_not_mask_skills():
    ok = await run(STRONG, candidate_name="Resume")
    bad = await run(STRONG, candidate_name="Docker Kafka Cv")
    assert ok["recommendation"] == "Shortlist"
    assert bad["overall_match_score"] == ok["overall_match_score"] == 100
    assert bad["recommendation"] == ok["recommendation"]
    assert bad["candidate_name"] == "Docker Kafka Cv"  # display is unchanged


@pytest.mark.parametrize("name", ["Docker Kafka Cv", "Resume", "Final CV v2", "Backend Engineer",
                                  "Python Developer Resume", "Candidate", "Go", "Kubernetes Aws"])
def test_masking_names_ignores_skill_role_and_generic_names(name):
    assert masking_names("no header here\nsecond line", name) == []
    out = redact_for_scoring("Skills: Docker, Kafka, Python, Go, AWS, Kubernetes\nResume", candidate_name=name)
    assert "Docker" in out and "Kafka" in out and "Python" in out and "[CANDIDATE]" not in out


def test_real_name_is_still_masked_from_header_or_explicit_name():
    text = "Priya Raman\npriya@example.com\nBuilt Kafka pipelines. Priya led the team."
    out = redact_for_scoring(text, candidate_name="Docker Kafka Cv")
    assert "Priya" not in out and "Raman" not in out and "Kafka" in out
    out2 = redact_for_scoring("Reached out by Dana Whitfield about FastAPI.", candidate_name="Dana Whitfield")
    assert "Dana" not in out2 and "Whitfield" not in out2 and "FastAPI" in out2


# --- 3. Redaction gaps ----------------------------------------------------------------

@pytest.mark.parametrize("leak", [
    "Born 1996 (age 29)", "age 29", "29 years old", "married", "Indian national",
    "Hindu Students Association", "IIT Bombay", "Stanford", "MIT", "IIT-Delhi",
    "Society of Women Engineers", "Mr Rao",
])
def test_inline_protected_attributes_are_masked(leak):
    out = redact_for_scoring(f"Jordan Lee\nj@x.io\nBuilt FastAPI services. {leak}. More Python.")
    assert leak not in out, out
    assert "FastAPI" in out and "Python" in out
    assert has_protected_attributes(f"Built services. {leak}.")


def test_pronouns_are_masked_but_job_text_survives():
    out = redact_for_scoring("Jordan Lee\nj@x.io\nShe led the team and her code shipped via Docker.")
    assert "She " not in out and " her " not in out
    assert "Docker" in out and "led the team" in out


@pytest.mark.parametrize("keep", [
    "Released under the MIT License", "Used Stanford CoreNLP for parsing",
    "Reviewed a national rollout with a single team", "Managed 29 engineers",
    "Maintained a 12 year old Python codebase",
])
def test_job_relevant_text_is_not_masked(keep):
    assert redact_for_scoring(f"Jordan Lee\nj@x.io\n{keep}").endswith(keep)


# --- 4. Education and experience -------------------------------------------------------

@pytest.mark.parametrize("text", ["Diploma in Information Systems", "Chess Clubs, Coding Clubs",
                                  "to be or not to be", "Systems"])
def test_non_degrees_do_not_match_as_a_degree(text):
    lvl = degree_level(text)
    assert lvl is None or lvl[0] == 0  # a diploma is a diploma, never a degree


@pytest.mark.parametrize("text,rank", [
    ("BSc Computer Science", 1), ("B.Sc. in Physics", 1), ("B.E. Computer Engineering", 1),
    ("BE in IT", 1), ("BCA, Pune", 1), ("B.Tech in IT", 1), ("Bachelor's in CS", 1),
    ("PhD in ML", 3), ("Ph.D. Statistics", 3), ("MCA", 2), ("M.Tech", 2), ("MBA", 2),
    ("Master of Science", 2), ("Diploma in Electronics", 0),
])
def test_degrees_are_recognised(text, rank):
    assert degree_level(text)[0] == rank


def _edu(blob):
    reqs = [{"id": "R1", "requirement": "Bachelor's degree in a computing field",
             "category": "education", "priority": "Must Have", "min_years": None}]
    resume = {"education": [blob], "skills": [], "projects": [], "experience": [], "total_years": 0}
    return match_requirements(reqs, resume)[0]


def test_diploma_in_information_systems_is_not_a_bachelor():
    assert _edu("Diploma in Information Systems, Lakeside Poly")["status"] == NOT_ENOUGH
    assert _edu("BSc Computer Science")["status"] == MATCHED
    assert _edu("BCA, Pune")["status"] == MATCHED


def test_months_count_so_jan_2022_to_dec_2024_is_three_years():
    r_ = structure_resume("Experience\nBackend Engineer at Acme, Jan 2022 - Dec 2024\n- Built APIs\n")
    assert r_["total_years"] == 3.0
    reqs = [{"id": "R1", "requirement": "3+ years of professional experience",
             "category": "min experience", "priority": "Must Have", "min_years": 3}]
    assert match_requirements(reqs, r_)[0]["status"] == MATCHED


def test_year_only_ranges_keep_their_old_value_and_overlaps_are_merged():
    assert total_years([{"start": "2019", "end": "2021"}]) == 2.0
    both = [{"start": "2019", "end": "2022"}, {"start": "2020", "end": "2021"}]
    assert total_years(both) == 3.0


def test_present_uses_the_clock_not_a_hard_coded_year():
    exp = [{"start": "2024", "end": "Present", "start_month": 1}]
    assert total_years(exp, today=date(2030, 1, 15)) == 6.08
    assert total_years(exp, today=date(2025, 1, 1)) == 1.08
    assert total_years(exp) == round(((date.today().year - 2024) * 12 + date.today().month) / 12, 2)


# --- 5. JD requirements are never silently dropped -------------------------------------

JD_WITH_UNKNOWNS = JD + "\nMust have\n- Terraform and Redis in production\n- GraphQL APIs\n- Rust\n"


def test_unrecognised_jd_lines_are_listed():
    lines = unrecognised_jd_lines(JD_WITH_UNKNOWNS)
    assert lines == ["Terraform and Redis in production", "GraphQL APIs", "Rust"] or \
        {"Terraform and Redis in production", "GraphQL APIs", "Rust"} <= set(lines)
    assert unrecognised_jd_lines(JD) == []  # the sample JD is fully covered


async def test_unrecognised_lines_reach_the_report_and_the_reasons():
    out = await run(STRONG, jd=JD_WITH_UNKNOWNS)
    ext = out["extensions"]
    assert any("Rust" in x for x in ext["unrecognised_jd_lines"])
    assert any("partial" in x for x in ext["recommendation_reasons"])
    UnifiedReport.model_validate(out)
    clean = await run(STRONG)
    assert clean["extensions"]["unrecognised_jd_lines"] == []
    assert not any("partial" in x for x in clean["extensions"]["recommendation_reasons"])


def test_recognised_requirements_are_unchanged_by_the_scan():
    assert [q["requirement"] for q in extract_requirements(JD)][:5] == [
        "Python", "FastAPI", "PostgreSQL", "Kafka", "Docker"]
