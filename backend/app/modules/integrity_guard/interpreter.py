"""Module A interpreter: LLM explains, Python decides (Spec 8.2)."""
from __future__ import annotations

from ...llm.client import LLMClient
from ...llm.prompts_common import UNTRUSTED_DATA_RULE, wrap_untrusted
from .prompt import SYSTEM_PROMPT

ACTIONS = ("proceed", "flag_for_review", "disqualify_review")
GUILT_WORDS = ("fraud", "fake", "cheat", "dishonest", "liar", "lying", "guilty")


def _benign_output(base_score: float) -> dict:
    return {
        "headline": "This document passed its integrity check with no findings.",
        "intent": "benign",
        "intent_reasoning": "The scanner reported no findings for this document.",
        "manipulation_attempted": False,
        "findings": [],
        "score_adjustment": {
            "base_score": base_score,
            "penalty": 1.0,
            "adjusted_score": round(base_score, 1),
            "explanation": "No integrity findings, so the score is unchanged.",
        },
        "recommended_action": "proceed",
        "audit_log_entry": "Integrity scan completed with no findings; no score adjustment applied.",
    }


async def interpret(scanner: dict, base_score: float, jd_text: str, llm: LLMClient) -> dict:
    if not scanner["flags"]:
        return _benign_output(base_score)

    payload = {
        "scanner_report": {
            **{k: v for k, v in scanner.items() if k not in ("hidden_text", "flags")},
            "flags": [
                {**f, "evidence": wrap_untrusted(f.get("evidence", ""))} for f in scanner["flags"]
            ],
            "hidden_text": wrap_untrusted(scanner.get("hidden_text", "")),
        },
        "base_score": base_score,
        "job_description": jd_text[:4000],
        # The mock provider reads these two directly.
        "verdict": scanner["verdict"],
        "penalty": scanner["penalty"],
        "flags": scanner["flags"],
    }
    try:
        result = (
            await llm.complete_json(
                f"{UNTRUSTED_DATA_RULE}\n\n{SYSTEM_PROMPT}", payload, task="integrity_interpret"
            )
        ).data
    except Exception:  # noqa: BLE001 - template fallback, never crash the demo
        result = _benign_output(base_score)
        result["headline"] = f"The integrity scan returned verdict '{scanner['verdict']}'."
    return enforce_guardrails(result, scanner, base_score)


def enforce_guardrails(result: dict, scanner: dict, base_score: float) -> dict:
    """Code-enforced rules the LLM may not override (Spec 8.2)."""
    codes = {f["code"] for f in scanner["flags"]}
    severities = [f["severity"] for f in scanner["flags"] if f["code"] != "OCR_LAYER"]
    out = dict(result)

    # Arithmetic is recomputed in Python; LLM arithmetic is discarded.
    penalty = float(scanner["penalty"])
    out["score_adjustment"] = {
        "base_score": round(base_score, 1),
        "penalty": penalty,
        "adjusted_score": round(base_score * penalty, 1),
        "explanation": result.get("score_adjustment", {}).get(
            "explanation", "The scanner's fixed penalty was applied to the base score."
        ),
    }

    # Intent.
    if codes <= {"OCR_LAYER"}:
        intent, action = "benign", "proceed"
    elif "INJECTION_HIDDEN" in codes or len([s for s in severities if s == "high"]) >= 2:
        intent, action = "deliberate", "disqualify_review"
    elif any(s in ("medium", "high") for s in severities):
        intent, action = "questionable", "flag_for_review"
    else:
        intent, action = "benign", "proceed"
    if len(severities) == 1 and severities[0] == "low":
        intent = "questionable" if intent == "deliberate" else intent
    out["intent"] = intent
    out["recommended_action"] = action if action in ACTIONS else "flag_for_review"

    hidden = scanner.get("hidden_text", "")
    out["manipulation_attempted"] = "INJECTION_HIDDEN" in codes or bool(
        hidden and "you are" in hidden.lower()
    )

    # No invented findings; every scanner flag explained exactly once.
    explained = {f.get("code"): f for f in out.get("findings", []) if f.get("code") in codes}
    out["findings"] = [
        explained.get(
            f["code"],
            {
                "code": f["code"],
                "plain_explanation": f["detail"],
                "why_it_matters": "It may affect an automated score.",
                "benign_alternative": "None - this has no legitimate use.",
            },
        )
        for f in scanner["flags"]
    ]

    # No guilt language anywhere in the prose.
    for key in ("headline", "intent_reasoning", "audit_log_entry"):
        text = str(out.get(key, ""))
        if any(w in text.lower() for w in GUILT_WORDS):
            out[key] = "The file contains formatting that could distort an automated score."
    out["penalty"] = penalty
    out["human_decides"] = "A human recruiter always makes the final call."
    return out
