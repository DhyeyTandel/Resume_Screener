"""End-to-end pipeline (Spec 1.2). Quarantine, parallel C/B, deterministic aggregation."""
from __future__ import annotations
import asyncio
import time
import uuid

from ..config import cfg
from ..llm.client import LLMClient
from ..llm.redaction import redact_for_scoring
from ..modules.authenticity_engine.engine import assess
from ..modules.core_screening.core import extract_requirements, match_requirements, structure_resume
from ..modules.integrity_guard.interpreter import interpret
from ..modules.integrity_guard.scanner import scan
from ..modules.interview_questions.generator import generate_interview_questions
from ..parsing.loader import ParsedDoc, UnreadableFile, from_text, load
from ..policy.recommendation import decide
from ..policy.scoring import apply_penalty, compose
from ..schemas.vocab import EXACT, NO_EVIDENCE, evidence_level, jd_priority, weakest

FAIRNESS_NOTICE = (
    "This is a decision-support tool. A human recruiter must review all recommendations. "
    "Do not use protected characteristics or proxies in screening."
)
AI_DISCLAIMER = (
    "AI-writing indicators are uncertain and should not be used as a sole hiring decision."
)


class Stage:
    def __init__(self, meta: dict, name: str) -> None:
        self.meta, self.name, self.t0 = meta, name, 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        entry = self.meta.setdefault(self.name, {})
        entry["latency_ms"] = int((time.perf_counter() - self.t0) * 1000)
        entry.setdefault("status", "error" if exc_type else "ok")
        if exc_type:
            entry["error"] = f"{exc_type.__name__}: {exc}"
        return True  # every stage degrades gracefully; the pipeline always returns


async def screen_candidate(
    *,
    jd_text: str,
    filename: str | None = None,
    data: bytes | None = None,
    pasted_text: str | None = None,
    candidate_name: str | None = None,
    github_username: str | None = None,
    portfolio_url: str | None = None,
    consent: dict | None = None,
    llm: LLMClient | None = None,
) -> dict:
    started = time.perf_counter()
    llm = llm or LLMClient()
    stages: dict = {}
    candidate_id = str(uuid.uuid4())[:8]

    # --- Stage 0: ingest ---------------------------------------------------
    with Stage(stages, "parse") as st:
        if data is not None and filename:
            doc = load(filename, data)
        else:
            doc = from_text(pasted_text or "", source="pasted")
        if not doc.visible_text.strip():
            raise UnreadableFile("The resume contains no readable text.", "Paste the text instead.")
    if stages["parse"]["status"] == "error":
        return _error_report(candidate_id, candidate_name, stages, started, llm)
    doc: ParsedDoc

    # --- Stage 1: Module A -------------------------------------------------
    with Stage(stages, "integrity"):
        scanner = scan(doc, jd_text)
    scanner = scanner if stages["integrity"]["status"] == "ok" else _clean_scan()

    # QUARANTINE: everything downstream sees visible text only.
    visible = doc.visible_text
    redacted = redact_for_scoring(visible, candidate_name=candidate_name)

    # --- Stage 2: Core -----------------------------------------------------
    with Stage(stages, "core"):
        requirements = extract_requirements(jd_text)
        parsed = structure_resume(redacted)
        matched = match_requirements(requirements, parsed)
        scores = compose(matched)
    if stages["core"]["status"] == "error":
        return _error_report(candidate_id, candidate_name, stages, started, llm)

    # --- Stages 3 & 4 in parallel -----------------------------------------
    async def run_authenticity() -> dict:
        with Stage(stages, "authenticity"):
            return assess(
                candidate_id,
                redacted,
                parsed,
                [r["requirement"] for r in requirements],
                consent=consent,
                sources={"github": github_username, "portfolio": portfolio_url},
            )

    async def run_narrative() -> dict:
        with Stage(stages, "narrative"):
            res = await llm.complete_json(
                "You write short, factual recruiter-facing prose from structured screening "
                "facts. You never invent facts and never state a hiring decision.",
                {
                    "matched": [r["requirement"] for r in matched if r["status"] == "Matched"],
                    "missing": [r["requirement"] for r in matched if r["status"] == "Missing"],
                    "base_score": scores["base_score"],
                    "total_requirements": len(matched),
                },
                task="summary",
            )
            return res.data

    auth_task = asyncio.create_task(run_authenticity())
    narr_task = asyncio.create_task(run_narrative())
    authenticity = await auth_task
    narrative = await narr_task
    authenticity = authenticity if isinstance(authenticity, dict) else None
    narrative = narrative if isinstance(narrative, dict) else {}

    # --- Stage 5: aggregation (deterministic) ------------------------------
    with Stage(stages, "integrity_interpret"):
        integrity = await interpret(scanner, scores["base_score"], jd_text, llm)
    if not isinstance(integrity, dict):
        integrity = {"recommended_action": "proceed", "penalty": 1.0, "findings": []}

    penalty = float(integrity.get("penalty", scanner["penalty"]))
    overall = apply_penalty(scores["base_score"], penalty)
    band = authenticity["band"] if authenticity else None
    recommendation, reasons = decide(
        overall_score=overall,
        score_confidence=scores["score_confidence"],
        requirements=matched,
        integrity_action=integrity.get("recommended_action", "proceed"),
        authenticity_band=band,
    )

    # --- Stage 6: Module D --------------------------------------------------
    with Stage(stages, "interview"):
        d_input = _build_d_input(matched, authenticity)
        questions = await generate_interview_questions(d_input, llm=llm)
    questions = questions if isinstance(questions, dict) else {
        "interview_questions": [], "skipped_strong_evidence": []
    }

    ai_level = _ai_level(authenticity)
    return {
        "candidate_name": candidate_name or "Candidate",
        "overall_match_score": overall,
        "recommendation": recommendation,
        "summary": narrative.get("summary", ""),
        "ai_text_indicators": {
            "level": ai_level,
            "disclaimer": AI_DISCLAIMER,
            "evidence": (
                [f"{k}: {v}" for k, v in authenticity["inflation_sub_signals"].items()]
                if authenticity else []
            ),
        },
        "requirement_match": [
            {
                "requirement": r["requirement"],
                "priority": r["priority"],
                "status": r["status"],
                "resume_evidence": r.get("resume_evidence", ""),
                "notes": r.get("notes", ""),
                "transferability": r.get("transferability") or {},
            }
            for r in matched
        ],
        "matched_skills": [r["requirement"] for r in matched if r["status"] == "Matched"],
        "missing_or_unclear_skills": [
            r["requirement"] for r in matched
            if r["status"] in ("Missing", "Not Enough Evidence")
        ],
        "technical_gaps": [
            f"{r['requirement']}: {r['notes']}" for r in matched if r["status"] == "Missing"
        ],
        "education_assessment": next(
            (r["notes"] for r in matched if r["category"] == "education"),
            "No education requirement was extracted from the job description.",
        ),
        "experience_assessment": next(
            (r["notes"] for r in matched if r["category"] == "min experience"),
            f"{parsed['total_years']:.0f} years computed from dated roles.",
        ),
        "strengths": narrative.get("strengths", []),
        "risks": narrative.get("risks", []),
        "interview_questions": [q["question"] for q in questions.get("interview_questions", [])],
        "fairness_notice": FAIRNESS_NOTICE,
        "extensions": {
            "candidate_id": candidate_id,
            "candidate_profile": parsed,
            "score_breakdown": {
                "base_score": scores["base_score"],
                "integrity_penalty": penalty,
                "score_confidence": scores["score_confidence"],
                "per_requirement_contribution": scores["per_requirement_contribution"],
            },
            "integrity": integrity,
            "authenticity": authenticity,
            "skill_intelligence": [
                r["transferability"] for r in matched if r.get("transferability")
            ],
            "interview_questions_detailed": questions,
            "recommendation_reasons": reasons,
            "human_review_required": True,
            "meta": {
                "stages": stages,
                "llm_provider": llm.active,
                "mock_mode": llm.mock_mode,
                "config_version": cfg("app.config_version"),
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "llm_calls": llm.calls,
            },
        },
    }


def _build_d_input(matched: list[dict], authenticity: dict | None) -> list[dict]:
    """Section 3.2 adapter: Module C (+B) -> Module D. Dedupe by skill, weakest wins."""
    by_skill: dict[str, dict] = {}
    b_status: dict[str, str] = {}
    if authenticity:
        for se in authenticity.get("skill_evidence", []):
            b_status[se["skill"].lower()] = se["status"]
    for r in matched:
        c = r.get("transferability")
        if not c:
            continue
        skill = c["required_skill"]
        depth = None
        level = evidence_level(
            c["classification"], b_status=b_status.get(skill.lower()), depth=depth
        )
        detail = c["recruiter_suggestion"]  # structured fields only, never hidden text
        item = {
            "skill": skill,
            "evidence_level": level,
            "detail": detail,
            "jd_priority": jd_priority(r["priority"]),
        }
        if skill in by_skill:
            item["evidence_level"] = weakest(
                [level, by_skill[skill]["evidence_level"]]
            )
        by_skill[skill] = item
    return list(by_skill.values())


def _ai_level(authenticity: dict | None) -> str:
    if not authenticity:
        return "Low"
    idx = authenticity["scores"]["inflation_index"]
    t = cfg("ai_indicators")
    return "Low" if idx <= t["low_max"] else "Medium" if idx <= t["medium_max"] else "High"


def _clean_scan() -> dict:
    return {
        "verdict": "clean", "confidence": 0, "penalty": 1.0, "flags": [],
        "hidden_text": "", "stats": {"hidden_words": 0, "pages": 0, "backends": []},
    }


def _error_report(cid, name, stages, started, llm) -> dict:
    err = next((s.get("error", "") for s in stages.values() if s.get("status") == "error"), "")
    return {
        "candidate_name": name or "Candidate",
        "overall_match_score": 0,
        "recommendation": "Review Manually",
        "summary": f"This file could not be screened: {err}",
        "ai_text_indicators": {"level": "Low", "disclaimer": AI_DISCLAIMER, "evidence": []},
        "requirement_match": [], "matched_skills": [], "missing_or_unclear_skills": [],
        "technical_gaps": [], "education_assessment": "", "experience_assessment": "",
        "strengths": [], "risks": [], "interview_questions": [],
        "fairness_notice": FAIRNESS_NOTICE,
        "extensions": {
            "candidate_id": cid, "status": "Error", "error": err,
            "remediation": "Re-export the file as a PDF or paste the resume text.",
            "candidate_profile": {}, "score_breakdown": {}, "integrity": {},
            "authenticity": None, "skill_intelligence": [],
            "interview_questions_detailed": {}, "recommendation_reasons": ["File could not be read."],
            "human_review_required": True,
            "meta": {"stages": stages, "llm_provider": llm.active, "mock_mode": llm.mock_mode,
                     "config_version": cfg("app.config_version"),
                     "latency_ms": int((time.perf_counter() - started) * 1000), "llm_calls": llm.calls},
        },
    }
