"""Canonical cross-module vocabulary (Spec Section 3). All mappings live here."""
from __future__ import annotations

MATCHED = "Matched"
PARTIAL = "Partially Matched"
MISSING = "Missing"
NOT_ENOUGH = "Not Enough Evidence"

EXACT = "Exact Match"
STRONGLY = "Strongly Transferable"
MODERATELY = "Moderately Transferable"
WEAKLY = "Weakly Transferable"
NO_EVIDENCE = "No Evidence"

# Section 3.1 - Module C classification -> Core requirement status.
_C_TO_CORE = {
    EXACT: MATCHED,
    STRONGLY: PARTIAL,
    MODERATELY: PARTIAL,
    WEAKLY: NOT_ENOUGH,
}


def classification_to_status(classification: str, *, resume_has_enough_content: bool) -> str:
    if classification in _C_TO_CORE:
        return _C_TO_CORE[classification]
    if classification == NO_EVIDENCE:
        return MISSING if resume_has_enough_content else NOT_ENOUGH
    raise ValueError(f"unknown classification: {classification!r}")


def requirement_value(status: str, transferability_score: float | None = None) -> float | None:
    """Section 3.5. None means EXCLUDED from numerator and denominator."""
    if status == MATCHED:
        return 1.0
    if status == PARTIAL:
        return 0.8 * transferability_score if transferability_score is not None else 0.5
    if status == MISSING:
        return 0.0
    if status == NOT_ENOUGH:
        return None
    raise ValueError(f"unknown status: {status!r}")


_EVIDENCE_RANK = {"strong_evidence": 3, "transferable": 2, "claimed": 1, "not_demonstrated": 0}
_B_DOWNGRADE = {"UNSUPPORTED", "WEAK", "CONTRADICTED", "UNVERIFIABLE"}


def evidence_level(classification: str, *, b_status: str | None, depth: str | None) -> str:
    """Section 3.2 - Module C (+ optional Module B) -> Module D evidence_level."""
    if classification == EXACT:
        verified = b_status in ("VERIFIED", "CORROBORATED")
        b_unavailable = b_status is None
        if verified or (b_unavailable and depth != "LISTED_ONLY"):
            level = "strong_evidence"
        else:
            level = "claimed"
    elif classification in (STRONGLY, MODERATELY, WEAKLY):
        level = "transferable"
    elif classification == NO_EVIDENCE:
        level = "not_demonstrated"
    else:
        raise ValueError(f"unknown classification: {classification!r}")
    # Module B concerns pull an item back to 'claimed' - the weakest level wins.
    if b_status in _B_DOWNGRADE and _EVIDENCE_RANK[level] > _EVIDENCE_RANK["claimed"]:
        level = "claimed"
    return level


def weakest(levels: list[str]) -> str:
    return min(levels, key=lambda lv: _EVIDENCE_RANK[lv])


def jd_priority(priority: str) -> str:
    return "must_have" if priority == "Must Have" else "nice_to_have"
