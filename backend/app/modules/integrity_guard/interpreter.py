"""Module A interpreter: LLM explains, Python decides (Spec 8.2)."""
from __future__ import annotations

import re

from ...llm.client import LLMClient
from ...llm.prompts_common import UNTRUSTED_DATA_RULE, wrap_untrusted
from .prompt import SYSTEM_PROMPT
from .scanner import INFO_CODES

ACTIONS = ("proceed", "flag_for_review", "disqualify_review")
GUILT_WORDS = ("fraud", "fake", "cheat", "dishonest", "liar", "lying", "guilty")
# Whole words with their inflections. Substring matching fired on ordinary words in real
# model prose ("underlying", "familiar", "cheatsheet") and replaced valid explanations.
GUILT_RE = re.compile(
    r"\b(fraud\w*|fak(?:e|ed|es|ing)|cheat(?:s|ed|er|ers|ing)?|dishonest\w*|liars?|lying|guilt(?:y|ily)?)\b",
    re.I,
)


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
    except Exception:
        result = _benign_output(base_score)
        result["headline"] = f"The integrity scan returned verdict '{scanner['verdict']}'."
    return enforce_guardrails(result, scanner, base_score)


def enforce_guardrails(result: dict, scanner: dict, base_score: float) -> dict:
    """Code-enforced rules the LLM may not override (Spec 8.2)."""
    codes = {f["code"] for f in scanner["flags"]}
    severities = [f["severity"] for f in scanner["flags"] if f["code"] not in INFO_CODES]
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
    if codes <= INFO_CODES:
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

    # Spec 8.2: quote at most 15 words of hidden text, always labelled. Built here from
    # the scanner's own (already <=15-word) evidence, never from model output, so the
    # recruiter sees exactly what the file contained and the label marks it as inert data.
    evidence_by_code = {f["code"]: f.get("evidence", "") for f in scanner["flags"]}
    for f in out["findings"]:
        ev = evidence_by_code.get(f["code"], "")
        if f["code"] in ("HIDDEN_TEXT", "INJECTION_HIDDEN") and ev:
            words = ev.split()
            quoted = " ".join(words[:15]) + ("..." if len(words) > 15 and not ev.endswith("...") else "")
            f["quoted_evidence"] = f'hidden text reads: "{quoted}"'
        else:
            f.pop("quoted_evidence", None)

    # No guilt language anywhere in the prose.
    for key in ("headline", "intent_reasoning", "audit_log_entry"):
        text = str(out.get(key, ""))
        if GUILT_RE.search(text):
            out[key] = "The file contains formatting that could distort an automated score."
    out["penalty"] = penalty
    out["human_decides"] = "A human recruiter always makes the final call."
    return out
