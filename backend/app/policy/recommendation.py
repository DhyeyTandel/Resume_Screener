"""Deterministic recommendation policy (Spec 3.6). The LLM only writes prose."""
from __future__ import annotations

from ..config import cfg
from .scoring import missing_must_haves

SHORTLIST = "Shortlist"
REVIEW = "Review Manually"
NOT_RECOMMENDED = "Not Recommended"


def decide(
    *,
    overall_score: int,
    score_confidence: float,
    requirements: list[dict],
    integrity_action: str = "proceed",
    authenticity_band: str | None = None,
) -> tuple[str, list[str]]:
    p = cfg("policy")
    reasons: list[str] = []
    missing = missing_must_haves(requirements)

    forced_review = []
    if integrity_action in ("flag_for_review", "disqualify_review"):
        forced_review.append(f"Integrity scan recommends '{integrity_action}'.")
    if authenticity_band == "NEEDS_VERIFICATION":
        forced_review.append("Authenticity assessment is NEEDS_VERIFICATION.")

    if overall_score < float(p["not_recommended_max"]):
        reasons.append(f"Score {overall_score} is below the not-recommended threshold "
                       f"{p['not_recommended_max']}.")
        return NOT_RECOMMENDED, reasons + forced_review
    if len(missing) >= int(p["not_recommended_missing_must_haves"]) and score_confidence >= float(
        p["min_score_confidence"]
    ):
        reasons.append(
            f"{len(missing)} must-have requirements have no evidence: {', '.join(missing)}."
        )
        return NOT_RECOMMENDED, reasons + forced_review

    if forced_review:
        return REVIEW, forced_review

    if (
        overall_score >= float(p["shortlist_min"])
        and not missing
        and score_confidence >= float(p["min_score_confidence"])
        and integrity_action == "proceed"
        and authenticity_band != "NEEDS_VERIFICATION"
    ):
        reasons.append(
            f"Score {overall_score} meets the shortlist threshold {p['shortlist_min']} "
            f"with no missing must-haves and confidence {score_confidence}."
        )
        return SHORTLIST, reasons

    if missing:
        reasons.append(f"Must-have not evidenced: {', '.join(missing)}.")
    if score_confidence < float(p["min_score_confidence"]):
        reasons.append(
            f"Score confidence {score_confidence} is below {p['min_score_confidence']}; "
            "too much of the resume could not be judged."
        )
    if overall_score < float(p["shortlist_min"]):
        reasons.append(f"Score {overall_score} is below the shortlist threshold "
                       f"{p['shortlist_min']}.")
    return REVIEW, reasons or ["No policy rule fired for shortlist or not-recommended."]
