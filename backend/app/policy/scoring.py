"""Deterministic score composition (Spec 3.5). Never trusted to an LLM."""
from __future__ import annotations

from ..config import cfg
from ..schemas.vocab import MISSING, NOT_ENOUGH, requirement_value


def weight_of(priority: str) -> float:
    w = cfg("core.weights")
    return float(w["must_have"] if priority == "Must Have" else w["preferred"])


def compose(requirements: list[dict]) -> dict:
    """requirements: [{requirement, priority, status, transferability_score?}]"""
    num = den = total_w = excluded_w = 0.0
    contributions = []
    for r in requirements:
        w = weight_of(r["priority"])
        total_w += w
        value = requirement_value(r["status"], r.get("transferability_score"))
        if value is None:
            excluded_w += w
            contributions.append(
                {"requirement": r["requirement"], "weight": w, "value": None, "excluded": True}
            )
            continue
        num += w * value
        den += w
        contributions.append(
            {
                "requirement": r["requirement"],
                "weight": w,
                "value": round(value, 3),
                "contribution": round(w * value, 3),
                "excluded": False,
            }
        )
    # den == 0 means every requirement was "Not Enough Evidence" (Spec 2.4, 3.5): there is no
    # honest number. base_score stays a numeric 0.0 so the contract keeps its shape, but
    # `score_assessable` is False and consumers must not read the 0 as a match result.
    assessable = den > 0
    base = 100 * num / den if assessable else 0.0
    confidence = 1 - (excluded_w / total_w) if total_w else 0.0
    return {
        "score_assessable": assessable,
        "base_score": round(base, 1),
        "score_confidence": round(confidence, 2),
        "per_requirement_contribution": contributions,
    }


def is_assessable(requirements: list[dict]) -> bool:
    """True when at least one requirement carries a value (Spec 3.5 denominator > 0)."""
    return any(
        requirement_value(r["status"], r.get("transferability_score")) is not None
        for r in requirements
    )


def apply_penalty(base_score: float, penalty: float) -> int:
    return round(base_score * penalty)


def missing_must_haves(requirements: list[dict]) -> list[str]:
    return [
        r["requirement"]
        for r in requirements
        if r["priority"] == "Must Have" and r["status"] == MISSING
    ]


def unresolved_must_haves(requirements: list[dict]) -> list[str]:
    return [
        r["requirement"]
        for r in requirements
        if r["priority"] == "Must Have" and r["status"] == NOT_ENOUGH
    ]
