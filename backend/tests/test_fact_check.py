"""Tests for the deterministic narrative fact-consistency check (offline, mock LLM only)."""
import re
from pathlib import Path

import pytest

from app.llm.client import LLMClient
from app.llm.fact_check import check_narrative
from app.pipeline.orchestrator import screen_candidate
from tests.fixtures.github_fixtures import make_fetch

ROOT = Path(__file__).resolve().parents[2]
JD = (ROOT / "sample_data/jd_backend_engineer.txt").read_text()
PASTED = {
    "strong match": ROOT / "sample_data/resumes/01_strong_match.txt",
    "transferable": ROOT / "sample_data/resumes/02_transferable.txt",
    "contradicted + GitHub (LLM judge path)": ROOT / "sample_data/resumes/06_inflated_contradicted.txt",
}
PDF = ROOT / "backend/tests/fixtures/pdfs/injection_hidden.pdf"
CASE_ORDER = ["strong match", "transferable", "hidden-injection PDF",
              "contradicted + GitHub (LLM judge path)"]

FACTS = {
    "matched": ["Python", "Docker", "3+ years of professional experience",
                "Bachelor's degree in a computing field", "Pytest"],
    "missing": ["Kafka", "AWS"],
    "base_score": 72.6,
    "total_requirements": 10,
}


class _Capture(LLMClient):
    facts: dict | None = None

    async def complete_json(self, system, user, *, task, **kw):  # type: ignore[override]
        if task == "summary":
            _Capture.facts = user
        return await super().complete_json(system, user, task=task, **kw)


@pytest.fixture(scope="module")
async def case_facts():
    """Facts per smoke case, reconstructed offline with the mock provider."""
    out = {}
    for name in CASE_ORDER:
        kw: dict = {}
        if name == "hidden-injection PDF":
            kw = {"filename": "injection_hidden.pdf", "data": PDF.read_bytes()}
        else:
            kw = {"pasted_text": PASTED[name].read_text()}
        if name.startswith("contradicted"):
            kw.update(github_username="derek", github_fetch=make_fetch("derek"))
        res = await screen_candidate(jd_text=JD, llm=_Capture("mock"), **kw)
        out[name] = (_Capture.facts, res)
    return out


def _report_samples(report: str) -> list[str]:
    sec = report.split("## Sample real-model prose", 1)[1]
    return re.findall(r"^- Summary: (.+)$", sec, flags=re.M)


@pytest.mark.parametrize("report", ["llm_smoke_report.md", "llm_smoke_report_openrouter.md"])
async def test_real_model_samples_have_no_violations(report, case_facts):
    summaries = _report_samples((ROOT / "eval" / report).read_text())
    assert len(summaries) == len(CASE_ORDER)
    for name, summary in zip(CASE_ORDER, summaries, strict=True):
        facts, _ = case_facts[name]
        assert check_narrative({"summary": summary, "strengths": [], "risks": []}, facts) == [], name


async def test_mock_narratives_have_no_violations(case_facts):
    for name, (facts, res) in case_facts.items():
        narr = {"summary": res["summary"], "strengths": res["strengths"], "risks": res["risks"]}
        assert check_narrative(narr, facts) == [], name


def _types(narr, facts=FACTS, **kw):
    return {v["type"] for v in check_narrative(narr, facts, **kw)}


def test_strength_naming_missing_skill_is_caught():
    v = check_narrative({"summary": "", "strengths": ["Strong Kafka experience"], "risks": []}, FACTS)
    assert [x["type"] for x in v] == ["strength_contradicts_facts"]
    assert "Kafka" in v[0]["detail"]
    assert "strength_contradicts_facts" in _types(
        {"summary": "The candidate has strong Kafka experience and Python skills.", "strengths": [],
         "risks": []})


def test_strength_naming_matched_skill_is_fine():
    assert _types({"summary": "", "strengths": ["Evidence found for Python", "Docker"], "risks": []}) == set()


def test_risk_naming_matched_skill_is_caught():
    assert "risk_contradicts_facts" in _types(
        {"summary": "", "strengths": [], "risks": ["No resume evidence for Docker"]})
    assert "risk_contradicts_facts" in _types(
        {"summary": "However, they lack Docker experience.", "strengths": [], "risks": []})


def test_risk_naming_missing_skill_is_fine():
    assert _types({"summary": "", "strengths": [], "risks": ["No resume evidence for Kafka"]}) == set()


def test_wrong_score_is_caught_and_rounding_allowed():
    bad = {"summary": "The base score is 85 for this candidate.", "strengths": [], "risks": []}
    assert "score_mismatch" in _types(bad)
    ok = {"summary": "They reach a base score of 73.", "strengths": [], "risks": []}
    assert _types(ok) == set()
    facts = {**FACTS, "base_score": "73"}
    assert check_narrative(ok, facts) == []
    assert _types({"summary": "A base score of 72.6 out of 100.", "strengths": [], "risks": []}) == set()
    assert "score_mismatch" in _types({"summary": "Match score: 40%", "strengths": [], "risks": []})


def test_wrong_count_is_caught_and_valid_counts_allowed():
    assert "count_mismatch" in _types(
        {"summary": "They meet 9 of the 10 job requirements.", "strengths": [], "risks": []})
    assert "count_mismatch" in _types(
        {"summary": "They meet 5 of the 12 job requirements.", "strengths": [], "risks": []})
    assert "count_mismatch" in _types(
        {"summary": "Scored against 12 total requirements.", "strengths": [], "risks": []})
    assert _types({"summary": "They meet 5 of the 10 job requirements across 10 total requirements.",
                   "strengths": [], "risks": []}) == set()
    # Percent of requirements is not the score, and years of experience are not counts.
    assert _types({"summary": "Roughly 50% of requirements are covered, with 3+ years of experience.",
                   "strengths": [], "risks": []}) == set()


def test_alias_resolves():
    facts = {"matched": ["PostgreSQL"], "missing": ["Kafka"], "base_score": 60, "total_requirements": 2}
    assert check_narrative({"summary": "Has Postgres expertise.", "strengths": ["Postgres"],
                            "risks": []}, facts) == []
    types = _types({"summary": "", "strengths": [], "risks": ["No evidence of Postgres"]}, facts)
    assert types == {"risk_contradicts_facts"}
    missing_alias = {"matched": ["Python"], "missing": ["PostgreSQL"], "base_score": 50,
                     "total_requirements": 2}
    assert "strength_contradicts_facts" in _types(
        {"summary": "", "strengths": ["Postgres administration"], "risks": []}, missing_alias)


def test_hallucinated_unknown_skill_is_caught_at_low_severity():
    v = check_narrative({"summary": "They have Python and Kubernetes experience.", "strengths": [],
                         "risks": []}, FACTS)
    assert [(x["type"], x["severity"]) for x in v] == [("unknown_skill", "low")]
    assert "Kubernetes" in v[0]["detail"]
    # Not a known skill: ignored. Supplied through known_skills: caught.
    narr = {"summary": "They have Terraform experience.", "strengths": [], "risks": []}
    assert check_narrative(narr, FACTS) == []
    assert _types(narr, known_skills={"Terraform"}) == {"unknown_skill"}


def test_accusatory_and_decision_language_is_caught():
    assert "accusatory_language" in _types(
        {"summary": "The resume looks like fraud.", "strengths": [], "risks": []})
    assert "accusatory_language" in _types(
        {"summary": "", "strengths": [], "risks": ["Candidate is cheating the screen"]})
    for phrase in ("They should be hired.", "We should reject this application.",
                   "Do not hire this person.", "They must not be considered for the role."):
        assert "hiring_decision" in _types({"summary": phrase, "strengths": [], "risks": []}), phrase
    assert "hiring_decision" in _types({"summary": "ok", "strengths": [], "risks": [],
                                        "rationale": "Reject."})
    # Words that merely contain the stems are fine.
    assert _types({"summary": "Understands the underlying cheatsheet format and is familiar with it.",
                   "strengths": [], "risks": []}) == set()


def test_negated_sentences_are_not_false_positives():
    narr = {"summary": "They lack Kafka and AWS experience.", "strengths": [], "risks": []}
    assert check_narrative(narr, FACTS) == []
    narr = {"summary": "They have Python and Docker experience, but no Kafka or AWS experience.",
            "strengths": [], "risks": []}
    assert check_narrative(narr, FACTS) == []
    narr = {"summary": "No supporting evidence was found for Kafka, AWS. Gaps remain in AWS.",
            "strengths": [], "risks": ["Kafka and AWS are missing", "No evidence of AWS or Kafka"]}
    assert check_narrative(narr, FACTS) == []
    # Strong Python in one clause and a Kafka gap in the next must not cross-contaminate.
    narr = {"summary": "Strong Python skills, although Kafka is missing; they lack AWS.",
            "strengths": [], "risks": []}
    assert check_narrative(narr, FACTS) == []


def test_malformed_input_is_ignored():
    assert check_narrative({}, FACTS) == []
    assert check_narrative({"summary": None, "strengths": "x", "risks": [1]}, FACTS) == []
    assert check_narrative("nope", FACTS) == []  # type: ignore[arg-type]


_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
_WIRING_JD = (_ROOT / "sample_data/jd_backend_engineer.txt").read_text()
_WIRING_RESUME = (_ROOT / "sample_data/resumes/02_transferable.txt").read_text()


async def test_pipeline_discards_prose_that_contradicts_the_facts():
    """Wiring: a model that names a Missing requirement as a strength gets the fact-only
    template instead, with the violation (not the prose) recorded in meta."""
    from app.llm.client import LLMClient, LLMResult
    from app.pipeline.orchestrator import screen_candidate

    jd, resume = _WIRING_JD, _WIRING_RESUME  # transferable resume: Kafka is Missing

    class Lying(LLMClient):
        def __init__(self):
            super().__init__("mock")
            self.provider = "fake"
            self.chain = ["fake"]

        async def complete_json(self, system, user, *, task, **kw):
            if task == "summary":
                return LLMResult(data={"summary": "The candidate has strong Kafka expertise.",
                                       "strengths": ["Kafka"], "risks": [], "rationale": "x"},
                                 provider="fake", model="f", latency_ms=1)
            return await LLMClient("mock").complete_json(system, user, task=task, **kw)

    r = await screen_candidate(jd_text=jd, pasted_text=resume, llm=Lying())
    assert "strong Kafka expertise" not in r["summary"]
    nar = r["extensions"]["meta"]["stages"]["narrative"]
    assert "contradicted the facts" in nar["fallback"]
    assert any(v["type"] == "strength_contradicts_facts" for v in nar["fact_check"])
    assert "strong Kafka expertise" not in str(nar)  # the prose itself is never stored
    assert r["extensions"]["meta"]["schema_valid"] is True
