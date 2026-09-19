"""Module D: evidence gaps -> targeted, non-accusatory interview questions (Spec 12)."""
from __future__ import annotations
import time

from ...config import cfg
from ...llm.client import LLMClient
from ...llm.prompts_common import UNTRUSTED_DATA_RULE
from .prompt import SYSTEM_PROMPT

VALID_LEVELS = {"claimed", "not_demonstrated", "transferable"}
_ORDER = {"must_have": 0, "nice_to_have": 1}


def _validate(data: dict, expected: set[str]) -> None:
    qs = data.get("interview_questions")
    if not isinstance(qs, list):
        raise ValueError("interview_questions missing")
    got = [q.get("skill") for q in qs]
    if set(got) != expected:
        raise ValueError(f"skill mismatch: expected {sorted(expected)}, got {sorted(set(got))}")
    if len(got) != len(set(got)):
        raise ValueError("more than one question for a skill")
    for q in qs:
        if q.get("evidence_level") not in VALID_LEVELS:
            raise ValueError(f"bad evidence_level: {q.get('evidence_level')}")
        for field in ("question", "purpose", "risk_if_unanswered"):
            if not str(q.get(field, "")).strip():
                raise ValueError(f"empty field {field}")


async def generate_interview_questions(
    requirements: list[dict], llm: LLMClient | None = None, max_retries: int = 2
) -> dict:
    """Generate exactly one interview question per non-strong-evidence requirement.

    Args:
        requirements: items of {skill, evidence_level, detail, jd_priority}.
        llm: client to use; a mock-backed client is created when omitted.
        max_retries: retries on parse/validation failure.

    Returns:
        {"interview_questions": [...], "skipped_strong_evidence": [...]} - never raises.
    """
    started = time.perf_counter()
    skipped = sorted(
        {r["skill"] for r in requirements if r.get("evidence_level") == "strong_evidence"}
    )
    todo = [r for r in requirements if r.get("evidence_level") != "strong_evidence"]
    if not todo:  # no API call at all
        return {
            "interview_questions": [],
            "skipped_strong_evidence": skipped,
            "_latency_seconds": round(time.perf_counter() - started, 4),
            "_attempts": 0,
            "_model": None,
            "_usage": {},
        }

    todo.sort(key=lambda r: (_ORDER.get(r.get("jd_priority"), 1), r["skill"]))
    expected = {r["skill"] for r in todo}
    client = llm or LLMClient()
    last_raw = ""
    attempts = 0
    for attempt in range(1, max_retries + 2):
        attempts = attempt
        try:
            res = await client.complete_json(
                f"{UNTRUSTED_DATA_RULE}\n\n{SYSTEM_PROMPT}",
                {"requirements": todo},
                task="interview_questions",
                temperature=float(cfg("interview.temperature", 0.3)),
            )
            last_raw = res.data
            _validate(res.data, expected)
            return {
                "interview_questions": res.data["interview_questions"],
                "skipped_strong_evidence": skipped,
                "_latency_seconds": round(time.perf_counter() - started, 4),
                "_attempts": attempts,
                "_model": res.model,
                "_usage": res.usage,
            }
        except Exception as exc:  # noqa: BLE001 - retry, then degrade gracefully
            error = str(exc)
    return {
        "interview_questions": [],
        "skipped_strong_evidence": skipped,
        "error": error,
        "raw_output": last_raw,
        "_latency_seconds": round(time.perf_counter() - started, 4),
        "_attempts": attempts,
        "_model": None,
        "_usage": {},
    }
