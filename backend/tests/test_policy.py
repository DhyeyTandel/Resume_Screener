"""Deterministic scoring, penalty math and recommendation policy (Spec 3.5-3.6)."""
from app.policy.recommendation import NOT_RECOMMENDED, REVIEW, SHORTLIST, decide
from app.policy.scoring import apply_penalty, compose
from app.schemas.vocab import (MATCHED, MISSING, NOT_ENOUGH, PARTIAL, classification_to_status,
                               evidence_level, jd_priority, requirement_value, weakest)


def r(name, priority, status, t=None):
    return {"requirement": name, "priority": priority, "status": status, "transferability_score": t}


def test_not_enough_evidence_is_excluded_from_both_sides():
    out = compose([r("A", "Must Have", MATCHED), r("B", "Must Have", NOT_ENOUGH)])
    assert out["base_score"] == 100.0
    assert out["score_confidence"] == 0.5


def test_weights_and_partial_value():
    out = compose([r("A", "Must Have", MISSING), r("B", "Preferred", MATCHED)])
    assert out["base_score"] == round(100 * 1.0 / 3.0, 1)


def test_transferable_uses_point_eight_times_score():
    assert requirement_value(PARTIAL, 0.76) == 0.8 * 0.76
    assert requirement_value(PARTIAL) == 0.5
    assert requirement_value(NOT_ENOUGH) is None


def test_adjusted_score_is_round_base_times_penalty():
    for base, pen in [(83.4, 0.4), (100.0, 0.75), (62.5, 1.0)]:
        assert apply_penalty(base, pen) == round(base * pen)


def test_shortlist_requires_every_condition():
    reqs = [r("A", "Must Have", MATCHED)]
    assert decide(overall_score=90, score_confidence=1.0, requirements=reqs)[0] == SHORTLIST
    # Integrity flag forces at least review.
    assert decide(overall_score=90, score_confidence=1.0, requirements=reqs,
                  integrity_action="flag_for_review")[0] == REVIEW
    # Authenticity band forces at least review.
    assert decide(overall_score=90, score_confidence=1.0, requirements=reqs,
                  authenticity_band="NEEDS_VERIFICATION")[0] == REVIEW
    # Low confidence forces review.
    assert decide(overall_score=90, score_confidence=0.3, requirements=reqs)[0] == REVIEW


def test_insufficient_evidence_band_never_blocks_shortlist():
    reqs = [r("A", "Must Have", MATCHED)]
    assert decide(overall_score=90, score_confidence=1.0, requirements=reqs,
                  authenticity_band="INSUFFICIENT_EVIDENCE")[0] == SHORTLIST


def test_not_recommended_needs_two_missing_must_haves():
    one = [r("A", "Must Have", MISSING), r("B", "Must Have", MATCHED)]
    assert decide(overall_score=50, score_confidence=1.0, requirements=one)[0] == REVIEW
    two = [r("A", "Must Have", MISSING), r("B", "Must Have", MISSING), r("C", "Must Have", MATCHED)]
    assert decide(overall_score=50, score_confidence=1.0, requirements=two)[0] == NOT_RECOMMENDED


def test_vocab_mappings():
    assert classification_to_status("Strongly Transferable", resume_has_enough_content=True) == PARTIAL
    assert classification_to_status("Weakly Transferable", resume_has_enough_content=True) == NOT_ENOUGH
    assert classification_to_status("No Evidence", resume_has_enough_content=True) == MISSING
    assert classification_to_status("No Evidence", resume_has_enough_content=False) == NOT_ENOUGH
    assert jd_priority("Must Have") == "must_have"
    assert weakest(["strong_evidence", "claimed", "transferable"]) == "claimed"


def test_evidence_level_mapping():
    assert evidence_level("Exact Match", b_status="VERIFIED", depth=None) == "strong_evidence"
    assert evidence_level("Exact Match", b_status=None, depth="LISTED_ONLY") == "claimed"
    assert evidence_level("Exact Match", b_status="UNVERIFIABLE", depth=None) == "claimed"
    assert evidence_level("Strongly Transferable", b_status=None, depth=None) == "transferable"
    assert evidence_level("No Evidence", b_status=None, depth=None) == "not_demonstrated"
