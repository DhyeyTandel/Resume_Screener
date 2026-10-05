"""End-to-end pipeline (Spec 1.2). Quarantine, parallel C/B, deterministic aggregation."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable

from ..config import cfg
from ..llm.client import MOCK_TASKS, LLMClient, _model_for, _redact
from ..llm.fact_check import check_narrative
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


# User-facing stages (Spec 14.2 item 3), in display order.
UI_STAGES = ("Parse", "Integrity", "Match", "Skills", "Authenticity", "Questions")
_TERMINAL = ("ok", "error", "skipped")


class _StageEmitter:
    """Maps internal pipeline stages onto the six user-facing stages and calls `on_stage`.

    Mapping (internal stage -> user-facing stage):
      parse               -> Parse
      integrity           -> Integrity (started here)
      integrity_interpret -> Integrity (the scan and its plain-English interpretation are one
                             step, so Integrity stays "started" until the interpreter finishes.
                             If the scan itself fails, Integrity reports "error" at once and the
                             interpreter's outcome is not reported again.)
      core                -> Match and Skills. Requirement matching and skill transferability run
                             inside one core call, so Skills starts and finishes when core does.
      authenticity        -> Authenticity
      narrative           -> not shown. It writes prose only, runs alongside Authenticity, and a
                             failure is already recorded in meta.stages and degrades to an empty
                             summary.
      interview           -> Questions
    Stages never reached (an early exit after a parse or core failure) are reported as "skipped".
    A callback that raises is logged and ignored: progress reporting must never break screening.
    """

    def __init__(self, on_stage: Callable[[str, str], None] | None) -> None:
        self.on_stage = on_stage
        self.state: dict[str, str] = {}
        self.integrity_scan_ok = False

    def _emit(self, stage: str, status: str) -> None:
        self.state[stage] = status
        if self.on_stage is None:
            return
        try:
            self.on_stage(stage, status)
        except Exception:
            log.warning("on_stage callback raised for %s/%s; ignored", stage, status, exc_info=True)

    def __call__(self, name: str, status: str) -> None:
        if name == "parse":
            self._emit("Parse", status)
        elif name == "integrity":
            if status == "started":
                self._emit("Integrity", "started")
            elif status == "error":
                self._emit("Integrity", "error")
            else:
                self.integrity_scan_ok = True  # terminal status waits for the interpreter
        elif name == "integrity_interpret":
            if self.state.get("Integrity") == "started":
                self._emit("Integrity", status)
        elif name == "core":
            if status == "started":
                self._emit("Match", "started")
            elif status == "ok":
                self._emit("Match", "ok")
                self._emit("Skills", "started")
                self._emit("Skills", "ok")
            else:
                self._emit("Match", "error")
        elif name == "authenticity":
            self._emit("Authenticity", status)
        elif name == "interview":
            self._emit("Questions", status)

    def flush(self) -> None:
        """Close every stage that never finished (early exits)."""
        for stage in UI_STAGES:
            cur = self.state.get(stage)
            if cur in _TERMINAL:
                continue
            if cur == "started" and stage == "Integrity" and self.integrity_scan_ok:
                self._emit(stage, "ok")  # the scan ran; only the later interpretation was skipped
            else:
                self._emit(stage, "skipped")


class Stage:
    def __init__(
        self, meta: dict, name: str, hook: Callable[[str, str], None] | None = None
    ) -> None:
        self.meta, self.name, self.t0, self.hook = meta, name, 0.0, hook

    def __enter__(self):
        self.t0 = time.perf_counter()
        self._notify("started")
        return self

    def _notify(self, status: str) -> None:
        if self.hook is None:
            return
        try:
            self.hook(self.name, status)
        except Exception:
            log.warning("stage hook raised for %s/%s; ignored", self.name, status, exc_info=True)

    def __exit__(self, exc_type, exc, tb):
        entry = self.meta.setdefault(self.name, {})
        entry["latency_ms"] = int((time.perf_counter() - self.t0) * 1000)
        entry.setdefault("status", "error" if exc_type else "ok")
        if exc_type:
            entry["error"] = f"{exc_type.__name__}: {exc}"
        self._notify("error" if entry.get("status") == "error" else "ok")
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


async def screen_candidate(
    *args, on_stage: Callable[[str, str], None] | None = None, **kwargs
) -> dict:
    """Run the pipeline and validate whatever it returns (normal, error and no-text paths).

    `on_stage(stage, status)` is called live with a user-facing stage name (Parse, Integrity,
    Match, Skills, Authenticity, Questions) and a status of "started", "ok", "error" or
    "skipped". See `_StageEmitter` for how internal stages map onto those six.
    """
    emitter = _StageEmitter(on_stage)
    llm = kwargs.get("llm") or LLMClient()
    kwargs["llm"] = llm
    recorder = _ProvenanceRecorder(llm)
    try:
        report = await _screen_candidate(*args, _emit=emitter, **kwargs)
    finally:
        recorder.uninstall()
        emitter.flush()
    recorder.apply(report)
    return validate_report(report)


# --- LLM provenance ---------------------------------------------------------------------
# Which model actually wrote what. The recorder wraps complete_json on the ONE client used
# for this screening (instance attribute, removed afterwards), so LLMClient, its signature and
# its behaviour are untouched, and the client's type is preserved (Module D checks it).
_TASK_NAMES = {
    "summary": "narrative",
    "integrity_interpret": "integrity_interpret",
    "claim_judge": "claim_judge",
    "interview_questions": "interview_questions",
}
_TASK_ORDER = list(_TASK_NAMES.values())
_REASON_MAX = 300


class _ProvenanceRecorder:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.calls: list[dict] = []
        self._had_own = "complete_json" in vars(llm)
        self._orig = llm.complete_json
        llm.complete_json = self._complete_json  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self._had_own:
            self.llm.complete_json = self._orig  # type: ignore[method-assign]
        else:
            vars(self.llm).pop("complete_json", None)

    async def _complete_json(self, *args, task: str, **kwargs):
        try:
            res = await self._orig(*args, task=task, **kwargs)
        except Exception as exc:
            self.calls.append({"task": task, "provider": "template", "model": "deterministic-template",
                               "fell_back": True, "reason": _redact(str(exc)) or type(exc).__name__})
            raise
        errors = [_redact(str(e)) for e in (getattr(res, "errors", None) or [])]
        self.calls.append({"task": task, "provider": res.provider, "model": res.model,
                           "fell_back": bool(errors), "reason": "; ".join(errors) or None})
        return res

    def provenance(self, ext: dict) -> list[dict]:
        calls = list(self.calls)
        questions = ext.get("interview_questions_detailed") or {}
        if (not any(c["task"] == "interview_questions" for c in calls)
                and questions.get("_attempts") and questions.get("_model")):
            # Module D built its own client (Anthropic upgrade, Spec 6.2), so it was not seen.
            model = str(questions["_model"])
            mock = model == _model_for("mock")
            calls.append({"task": "interview_questions", "provider": "mock" if mock else "anthropic",
                          "model": model, "fell_back": False, "reason": None})
        out: list[dict] = []
        stages = (ext.get("meta") or {}).get("stages") or {}
        template_reason = (stages.get("narrative") or {}).get("fallback")
        for name in _TASK_ORDER:
            mine = [c for c in calls if _TASK_NAMES.get(c["task"], c["task"]) == name]
            # Calls complete in arbitrary order now; sort so ties and reason order are stable.
            mine.sort(key=lambda c: (c["provider"], c["model"], c["reason"] or ""))
            if not mine:
                continue
            tally: dict[tuple, int] = {}
            for c in mine:
                tally[(c["provider"], c["model"])] = tally.get((c["provider"], c["model"]), 0) + 1
            (provider, model), _n = max(tally.items(), key=lambda kv: kv[1])
            reasons = list(dict.fromkeys(c["reason"] for c in mine if c["reason"]))
            fell_back = any(c["fell_back"] for c in mine)
            if name == "narrative" and template_reason:
                reasons.append(f"{template_reason} (answered by {provider}/{model})")
                provider, model, fell_back = "template", "deterministic-template", True
            out.append({"task": name, "provider": provider, "model": model, "fell_back": fell_back,
                        "reason": "; ".join(reasons)[:_REASON_MAX] or None, "calls": len(mine)})
        return out

    def apply(self, report: dict) -> None:
        try:
            ext = report["extensions"]
            meta = ext["meta"]
            prov = self.provenance(ext)
            meta["provenance"] = prov
            if prov:
                weight: dict[str, int] = {}
                for p in prov:
                    weight[p["provider"]] = weight.get(p["provider"], 0) + p["calls"]
                meta["llm_provider"] = max(weight.items(), key=lambda kv: kv[1])[0]
                meta["mock_mode"] = all(p["provider"] in ("mock", "template") for p in prov)
        except Exception:
            log.warning("provenance could not be recorded", exc_info=True)


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
    _emit: Callable[[str, str], None] | None = None,
) -> dict:
    started = time.perf_counter()
    llm = llm or LLMClient()
    stages: dict = {}
    candidate_id = str(uuid.uuid4())[:8]

    # --- Stage 0: ingest ---------------------------------------------------
    no_text: NoTextLayer | None = None
    with Stage(stages, "parse", _emit):
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
    with Stage(stages, "integrity", _emit):
        scanner = scan(doc, jd_text)
    scanner = scanner if stages["integrity"]["status"] == "ok" else _clean_scan()

    # QUARANTINE: everything downstream sees visible text only.
    visible = doc.visible_text
    redacted = redact_for_scoring(visible, candidate_name=candidate_name)

    # --- Stage 2: Core -----------------------------------------------------
    with Stage(stages, "core", _emit):
        requirements = extract_requirements(jd_text)
        parsed = structure_resume(redacted)
        matched = match_requirements(requirements, parsed)
        scores = compose(matched)
    if stages["core"]["status"] == "error":
        return _error_report(candidate_id, candidate_name, stages, started, llm)

    # --- Stages 3, 4 and the integrity interpreter, all concurrent ---------
    # The interpreter reads only the scanner result, base_score and the JD, so it needs nothing
    # from authenticity or the narrative and starts as soon as core has produced base_score.
    # Module D (below) stays after authenticity: it consumes its results.
    async def run_interpreter() -> dict | None:
        with Stage(stages, "integrity_interpret", _emit):
            return await interpret(scanner, scores["base_score"], jd_text, llm)
        return None  # Stage swallowed an exception; the fallback below handles it

    async def run_authenticity() -> dict:
        with Stage(stages, "authenticity", _emit):
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
        with Stage(stages, "narrative", _emit):
            facts = {
                "matched": [r["requirement"] for r in matched if r["status"] == "Matched"],
                "missing": [r["requirement"] for r in matched if r["status"] == "Missing"],
                # Without these the model had to infer the rest: a real model wrote "meets 8 of
                # 10" when 7 were matched, counting a partial match as met.
                "partially_matched": [r["requirement"] for r in matched if r["status"] == "Partially Matched"],
                "not_enough_evidence": [r["requirement"] for r in matched
                                        if r["status"] == "Not Enough Evidence"],
                "base_score": scores["base_score"],
                "total_requirements": len(matched),
            }
            res = await llm.complete_json(
                "You write short, factual recruiter-facing prose from structured screening "
                "facts. You never invent facts and never state a hiring decision.\n\n"
                'Return ONLY JSON with exactly these keys: {"summary": "<2-4 sentences>", '
                '"strengths": ["<short phrase>", ...], "risks": ["<short phrase>", ...], '
                '"rationale": "<1 sentence>"}. Use only the facts given.',
                facts,
                task="summary",
            )
            data = res.data if isinstance(res.data, dict) else {}
            ok = (isinstance(data.get("summary"), str) and data["summary"].strip()
                  and isinstance(data.get("strengths", []), list)
                  and isinstance(data.get("risks", []), list))
            if ok:
                # Spec 2.8 / 9.7: the model writes prose from the facts and never invents them.
                # High-severity contradictions (a missing skill named as a strength, a wrong
                # score) discard the prose for the fact-only template; low-severity findings are
                # recorded only. Violation types and details are kept, never the prose itself.
                violations = check_narrative(data, {
                    **facts, "partial": facts["partially_matched"] + facts["not_enough_evidence"]})
                if violations:
                    stages.setdefault("narrative", {})["fact_check"] = [
                        {"type": v["type"], "severity": v["severity"], "detail": v["detail"]}
                        for v in violations]
                if any(v["severity"] == "high" for v in violations):
                    stages.setdefault("narrative", {})["fallback"] = (
                        "template: model prose contradicted the facts ("
                        + ", ".join(sorted({v["type"] for v in violations if v["severity"] == "high"})) + ")")
                    return MOCK_TASKS["summary"](facts)
                return data
            # A real model returned the wrong shape (measured: every report blank against a
            # local 7B model before the keys were stated). Fall back to the deterministic,
            # fact-only template rather than ship an empty explanation, and say so in meta.
            stages.setdefault("narrative", {})["fallback"] = "template: model output lacked the required keys"
            return MOCK_TASKS["summary"](facts)

    auth_task = asyncio.create_task(run_authenticity())
    narr_task = asyncio.create_task(run_narrative())
    integ_task = asyncio.create_task(run_interpreter())
    authenticity = await auth_task
    narrative = await narr_task
    integrity = await integ_task  # None if the interpreter raised (Stage swallows it)
    authenticity = authenticity if isinstance(authenticity, dict) else None
    narrative = narrative if isinstance(narrative, dict) else {}
    # Concurrent stages finish in arbitrary order; keep meta.stages in a fixed order.
    _order = ("parse", "integrity", "core", "authenticity", "narrative", "integrity_interpret")
    _sorted = {k: stages[k] for k in _order if k in stages}
    _sorted.update({k: v for k, v in stages.items() if k not in _sorted})
    stages.clear()
    stages.update(_sorted)

    # --- Stage 5: aggregation (deterministic) ------------------------------
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
    with Stage(stages, "interview", _emit):
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
