"""End-to-end pipeline (Spec 1.2). Quarantine, parallel C/B, deterministic aggregation."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from ..config import cfg
from ..llm.client import LLMClient
from ..llm.redaction import redact_for_scoring
from ..modules.authenticity_engine.engine import assess
from ..modules.core_screening.core import extract_requirements, match_requirements, structure_resume
from ..modules.integrity_guard.interpreter import enforce_guardrails, interpret
from ..modules.integrity_guard.scanner import scan
from ..modules.interview_questions.generator import generate_interview_questions
from ..parsing.loader import NoTextLayer, UnreadableFile, from_text, load
from ..policy.recommendation import decide
from ..policy.scoring import apply_penalty, compose
from ..schemas.report import VERIFICATION_GAPS_CONTRACT, UnifiedReport
from ..schemas.vocab import NOT_ENOUGH, evidence_level, jd_priority, weakest

FAIRNESS_NOTICE = (
    "This is a decision-support tool. A human recruiter must review all recommendations. "
    "Do not use protected characteristics or proxies in screening."
)
log = logging.getLogger(__name__)
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


def validate_report(report: dict) -> dict:
    """Validate against the unified schema (Spec 13) and record the outcome in meta.

    Never raises: a schema failure must not crash the pipeline (Spec: never crash the demo).
    The report is returned either way, with `schema_valid` and, on failure, `schema_errors`.
    """
    try:
        ext = report.get("extensions")
        auth = ext.get("authenticity") if isinstance(ext, dict) else None
        if isinstance(auth, dict):
            # Version tag for the verification_gaps item shape (Spec 3.3); additive sibling key.
            auth.setdefault("verification_gaps_contract", VERIFICATION_GAPS_CONTRACT)
        UnifiedReport.model_validate(report)
        valid, errors = True, []
    except Exception as exc:  # ValidationError, or a malformed report that is not even a dict
        valid = False
        errors = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in getattr(exc, "errors", lambda: [])()
        ] or [f"{type(exc).__name__}: {exc}"]
        log.warning("report failed schema validation: %s", errors[:5])
    try:
        meta = report["extensions"].setdefault("meta", {})
        meta["schema_valid"] = valid
        if not valid:
            meta["schema_errors"] = errors[:20]
    except Exception:
        pass  # a report too malformed to carry meta is still returned as-is
    return report


async def screen_candidate(*args, **kwargs) -> dict:
    """Run the pipeline and validate whatever it returns (normal, error and no-text paths)."""
    return validate_report(await _screen_candidate(*args, **kwargs))


async def _screen_candidate(
    *,
    jd_text: str,
    filename: str | None = None,
    data: bytes | None = None,
    pasted_text: str | None = None,
    candidate_name: str | None = None,
    github_username: str | None = None,
    portfolio_url: str | None = None,
    linkedin_export: dict | None = None,  # {"type": "pdf_export"|"structured_json", "content": ...}
    consent: dict | None = None,
    llm: LLMClient | None = None,
    github_fetch=None,  # test seam: inject a recorded fetch instead of a live one
    portfolio_fetch=None,  # test seam for the portfolio collector
) -> dict:
    started = time.perf_counter()
    llm = llm or LLMClient()
    stages: dict = {}
    candidate_id = str(uuid.uuid4())[:8]

    # --- Stage 0: ingest ---------------------------------------------------
    no_text: NoTextLayer | None = None
    with Stage(stages, "parse"):
        if data is not None and filename:
            try:
                doc = load(filename, data)
            except NoTextLayer as exc:  # a scan: reported as Not Enough Evidence, not an error
                no_text = exc
                stages["parse"] = {"note": "scan without a text layer"}
        else:
            doc = from_text(pasted_text or "", source="pasted")
        if no_text is None and not doc.visible_text.strip():
            raise UnreadableFile("The resume contains no readable text.", "Paste the text instead.")
    if no_text is not None:
        return _no_text_report(candidate_id, candidate_name, jd_text, stages, started, llm)
    if stages["parse"]["status"] == "error":
        return _error_report(candidate_id, candidate_name, stages, started, llm)

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
            return await assess(
                candidate_id,
                redacted,
                parsed,
                [r["requirement"] for r in requirements],
                consent=consent,
                sources={"github": github_username, "portfolio": portfolio_url,
                        "linkedin": linkedin_export},
                github_fetch=github_fetch,
                portfolio_fetch=portfolio_fetch,
                llm=llm,
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
    integrity = None  # stays None if the interpreter raises (Stage swallows it); handled below
    with Stage(stages, "integrity_interpret"):
        integrity = await interpret(scanner, scores["base_score"], jd_text, llm)
    if not isinstance(integrity, dict):
        # The interpreter only writes prose. Penalty, intent and action are deterministic
        # facts from the scanner and must not be lost when the interpreter fails: an attack
        # file must still get its 0.40 penalty and disqualify_review. enforce_guardrails
        # derives all of them from the scanner alone, with no LLM.
        integrity = enforce_guardrails(
            {
                "headline": "The integrity explanation was unavailable; the scanner's findings "
                "and penalty still apply.",
                "intent_reasoning": "Derived from the scanner's findings without the interpreter.",
                "findings": [],
                "audit_log_entry": f"Integrity scan returned verdict '{scanner['verdict']}'; the "
                "interpreter was unavailable and the scanner penalty was applied.",
            },
            scanner,
            scores["base_score"],
        )

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
    questions = None  # stays None if Module D raises (Stage swallows it); handled below
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
                # Module D can switch to Anthropic on its own when ANTHROPIC_API_KEY is set
                # (Spec 6.2), so "mock mode" is only claimed when no real model served anything.
                "mock_mode": llm.mock_mode and questions.get("_model") in (None, "mock-heuristic-v1"),
                "interview_model": questions.get("_model"),
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


NO_TEXT_NOTE = (
    "This file is a scan without a text layer, so no resume text could be read. "
    "That says nothing about the candidate; the evidence is simply not available to the screener."
)


def _no_text_report(cid, name, jd_text, stages, started, llm) -> dict:
    """A scanned PDF with no text layer and no OCR (Spec 7). Every requirement is
    "Not Enough Evidence" (never "Missing"), so the score is excluded and confidence is 0."""
    try:
        reqs = extract_requirements(jd_text)
    except Exception:
        reqs = []
    matched = [
        {
            "requirement": r["requirement"], "priority": r["priority"], "category": r["category"],
            "status": NOT_ENOUGH, "resume_evidence": "", "notes": NO_TEXT_NOTE, "transferability": {},
        }
        for r in reqs
    ]
    scores = compose(matched)
    reason = (
        "The file is a scan with no text layer and OCR was not available, so it needs a manual read."
    )
    return {
        "candidate_name": name or "Candidate",
        "overall_match_score": 0,
        "recommendation": "Review Manually",
        "summary": NO_TEXT_NOTE + " Open the original file and review it manually, or ask for a text-based copy.",
        "ai_text_indicators": {"level": "Low", "disclaimer": AI_DISCLAIMER, "evidence": []},
        "requirement_match": [
            {k: v for k, v in m.items() if k != "category"} for m in matched  # category is internal
        ],
        "matched_skills": [],
        "missing_or_unclear_skills": [r["requirement"] for r in matched],
        "technical_gaps": [],
        "education_assessment": NO_TEXT_NOTE,
        "experience_assessment": NO_TEXT_NOTE,
        "strengths": [],
        "risks": [reason],
        "interview_questions": [],
        "fairness_notice": FAIRNESS_NOTICE,
        "extensions": {
            "candidate_id": cid, "status": NOT_ENOUGH,
            "remediation": "Upload a text-based PDF or DOCX, paste the text, or review the scan by hand.",
            "candidate_profile": {},
            "score_breakdown": {
                "base_score": 0, "integrity_penalty": 1.0, "score_confidence": 0,
                "per_requirement_contribution": scores["per_requirement_contribution"],
            },
            "integrity": {
                "headline": "The integrity scan did not run because the file has no text layer.",
                "intent": "benign", "intent_reasoning": "No text was available to scan.",
                "manipulation_attempted": False, "findings": [],
                "recommended_action": "proceed", "penalty": 1.0,
            },
            "authenticity": None, "skill_intelligence": [],
            "interview_questions_detailed": {"interview_questions": [], "skipped_strong_evidence": []},
            "recommendation_reasons": [reason],
            "human_review_required": True,
            "meta": {"stages": stages, "llm_provider": llm.active, "mock_mode": llm.mock_mode,
                     "config_version": cfg("app.config_version"),
                     "latency_ms": int((time.perf_counter() - started) * 1000), "llm_calls": llm.calls},
        },
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
