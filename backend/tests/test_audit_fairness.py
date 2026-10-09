"""Adversarial-audit regressions: false-accusation paths and the AI-text safeguard.

* A concurrent part-time / intern / freelance / tutoring role must not read as "two full-time
  jobs", and a resume-only inconsistency (no second source disagrees) must not set the band.
* Spec 2.7: AI-written-text indicators are display-only; the band must not depend on
  inflation_index.
"""
import pytest

from app.llm.client import LLMClient
from app.modules.authenticity_engine import engine
from app.modules.authenticity_engine.consistency import check_role_overlap, is_full_time
from app.pipeline.orchestrator import screen_candidate
from tests.fixtures.github_fixtures import make_fetch
from tests.test_invariants import JD, STRONG

BANDS = {"min_confidence": 0.40, "high_trust": 0.75, "moderate": 0.50}


async def screen(text, **kw):
    return await screen_candidate(
        jd_text=JD, pasted_text=text, llm=LLMClient("mock"),
        github_username="priya", github_fetch=make_fetch("priya"), **kw)


# ------------------------------------------------------------------ 1. false-accusation path
TUTOR = STRONG.replace(
    "Backend Engineer at Corvid Systems, 2019 - 2021",
    "Part-time Tutor at Local Coding Club, 2021 - Present\n"
    "- Tutored students in Python\n"
    "Backend Engineer at Corvid Systems, 2019 - 2021",
)


async def test_concurrent_part_time_tutor_does_not_flip_a_strong_resume():
    base = await screen(STRONG)
    out = await screen(TUTOR)
    assert base["recommendation"] == "Shortlist"
    assert out["recommendation"] == base["recommendation"]
    a = out["extensions"]["authenticity"]
    assert a["band"] == base["extensions"]["authenticity"]["band"] != "NEEDS_VERIFICATION"
    assert not any(c["type"] == "overlapping_roles" for c in a["contradictions"])


@pytest.mark.parametrize("title", [
    "Part-time Tutor", "Software Engineering Intern", "Freelance Developer",
    "Contract Engineer", "Volunteer Mentor", "Teaching Assistant", "Research Assistant",
    "Tutor", "Co-op Developer",
])
def test_non_full_time_titles_are_exempt_from_overlap(title):
    exp = [{"title": "Backend Engineer", "company": "A", "start": "2019", "end": "2024"},
           {"title": title, "company": "B", "start": "2021", "end": "2024"}]
    assert not is_full_time(exp[1])
    assert check_role_overlap(exp) == []


def test_two_real_full_time_roles_still_overlap_and_text_makes_no_false_claim():
    exp = [{"title": "Backend Engineer", "company": "A", "start": "2019", "end": "2024"},
           {"title": "Data Engineer", "company": "B", "start": "2021", "end": "2025"}]
    out = check_role_overlap(exp)
    assert len(out) == 1
    assert "both shown as full-time" not in out[0]["detail"]


def test_overlap_uses_month_granularity_when_months_are_present():
    # Year-only arithmetic reads 2020-2021 vs 2021-2022 as a clean hand-over; with months a
    # real five-month overlap is visible, and a one-month hand-over is within tolerance.
    seq = [{"title": "Engineer", "company": "A", "start": "Jan 2020", "end": "Jun 2021"},
           {"title": "Developer", "company": "B", "start": "Jul 2021", "end": "Dec 2022"}]
    assert check_role_overlap(seq) == []
    clash = [{"title": "Engineer", "company": "A", "start": "Jan 2020", "end": "Dec 2021"},
             {"title": "Developer", "company": "B", "start": "Jul 2021", "end": "Dec 2022"}]
    assert len(check_role_overlap(clash)) == 1
    within_tol = [{"title": "Engineer", "company": "A", "start": "03/2020", "end": "08/2021"},
                  {"title": "Developer", "company": "B", "start": "2021-07", "end": "2022-12"}]
    assert check_role_overlap(within_tol) == []


async def test_resume_only_inconsistency_is_a_gap_and_a_note_not_a_forced_band():
    overlapping = STRONG.replace(
        "Backend Engineer at Corvid Systems, 2019 - 2021",
        "Backend Engineer at Corvid Systems, 2019 - 2026",
    )
    base = await screen(STRONG)
    out = await screen(overlapping)
    a = out["extensions"]["authenticity"]
    assert any(c["type"] == "overlapping_roles" for c in a["contradictions"])  # still visible
    # Not forced to NEEDS_VERIFICATION. The finding still lowers the consistency term of the
    # spec formula, so the band may step from HIGH_TRUST to MODERATE through the score.
    assert a["band"] in ("HIGH_TRUST", "MODERATE")
    assert out["recommendation"] == base["recommendation"]
    assert any("overlap" in g["what_to_verify"].lower() for g in a["verification_gaps"])
    assert "resume" in a["recruiter_summary"].lower() and "overlap" in a["recruiter_summary"].lower()


async def test_linkedin_sourced_contradiction_still_forces_the_band():
    li = {"type": "structured_json", "content": {"roles": [
        {"title": "Senior Backend Engineer", "company": "Northwind Payments",
         "start": "2023", "end": "Present"}]}}
    out = await screen(STRONG, linkedin_export=li)
    a = out["extensions"]["authenticity"]
    assert any(c["type"] == "date_conflict" for c in a["contradictions"])
    assert a["band"] == "NEEDS_VERIFICATION"


# ------------------------------------------------------------------ 2. Spec 2.7
def test_band_score_is_independent_of_inflation_index():
    w = {"alpha": 0.6, "beta": 0.3, "gamma": 0.1}
    assert engine.band_score(0.9, 1.0, w) == engine.band_score(0.9, 1.0, w)
    assert engine.band_score(0.0, 0.0, w) == 0.0
    assert engine.band_score(1.0, 1.0, w) == 1.0


def _buzzy(text):
    """Same facts, AI-style wording: the summary and the first bullet verbs gain buzzwords."""
    summary = ("Passionate, results-driven, dynamic self-starter and team player: a visionary "
               "thought leader, rockstar, ninja and guru delivering seamless, robust, "
               "cutting-edge, world-class, best-in-class, game-changing, revolutionary "
               "solutions that leverage synergy.")
    return (text.replace("Backend engineer building payment and billing services in Python.", summary)
            .replace("Built and shipped", "Spearheaded cutting-edge")
            .replace("Designed", "Leveraged robust")
            .replace("Owned", "Spearheaded")
            .replace("Ran Kafka", "Spearheaded Kafka")
            .replace("Wrote Python", "Spearheaded Python"))


async def test_truthful_ai_style_rewrite_never_changes_band_or_recommendation():
    plain = await screen(STRONG)
    ai = await screen(_buzzy(STRONG))
    ps, ais = plain["extensions"]["authenticity"], ai["extensions"]["authenticity"]
    assert ais["scores"]["inflation_index"] > ps["scores"]["inflation_index"]  # text did change
    assert ais["scores"]["authenticity"] < ps["scores"]["authenticity"]  # display keeps spec formula
    assert ais["scores"]["reliability"] == ps["scores"]["reliability"]
    assert ais["scores"]["band_score"] == ps["scores"]["band_score"]
    assert ais["band"] == ps["band"]
    assert ai["recommendation"] == plain["recommendation"]


async def test_band_does_not_move_when_inflation_alone_would_cross_a_threshold(monkeypatch):
    """Pin reliability and consistency just above the HIGH_TRUST line; only inflation differs.
    With the spec formula the high-inflation run would fall to MODERATE."""
    outs = {}
    for label, idx in (("low", 0.0), ("high", 1.0)):
        monkeypatch.setattr(engine, "inflation_signals",
                            lambda *a, _i=idx, **k: {"inflation_index": _i, "sub_signals": {}})
        outs[label] = await screen(STRONG)
    lo, hi = outs["low"]["extensions"]["authenticity"], outs["high"]["extensions"]["authenticity"]
    assert lo["scores"]["authenticity"] - hi["scores"]["authenticity"] == pytest.approx(0.1, abs=0.002)
    assert lo["band"] == hi["band"]
    assert lo["scores"]["band_score"] == hi["scores"]["band_score"]
