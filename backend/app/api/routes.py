"""FastAPI surface (Spec 14.1)."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from ..config import cfg
from ..db.store import get_store
from ..llm.client import LLMClient
from ..llm.redaction import redact_for_scoring
from ..modules.authenticity_engine.engine import assess
from ..modules.core_screening.core import extract_requirements, structure_resume
from ..modules.interview_questions.generator import generate_interview_questions
from ..modules.skill_intelligence.transfer import analyze_skill
from ..pipeline.orchestrator import UI_STAGES, screen_candidate

router = APIRouter(prefix="/v1")

_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE_OUTPUT = _ROOT / "sample_output"
_SAMPLE_JD = _ROOT / "sample_data" / "jd_backend_engineer.txt"


# --- Live per-candidate progress (in memory only) ---------------------------
# {screening_id: [ {index, candidate_name, status, current_stage, stages: [{name, status}]} ]}
# Stage status: pending | running | done | error | skipped. Candidate status: pending | running |
# done | error. Not persisted: after a restart `progress` is simply empty for old screenings.
_PROGRESS: dict[str, list[dict]] = {}
_MAX_TRACKED = 200
_STATUS_MAP = {"started": "running", "ok": "done", "error": "error", "skipped": "skipped"}


def _new_progress(inputs: list[dict]) -> list[dict]:
    return [
        {
            "index": i,
            "candidate_name": item.get("candidate_name") or "Candidate",
            "status": "pending",
            "current_stage": None,
            "stages": [{"name": n, "status": "pending"} for n in UI_STAGES],
        }
        for i, item in enumerate(inputs)
    ]


def _register_progress(sid: str, inputs: list[dict]) -> list[dict]:
    _PROGRESS[sid] = _new_progress(inputs)
    while len(_PROGRESS) > _MAX_TRACKED:  # bound memory: drop the oldest screenings first
        _PROGRESS.pop(next(iter(_PROGRESS)))
    return _PROGRESS[sid]


def _stage_callback(entry: dict):
    def on_stage(stage: str, status: str) -> None:
        mapped = _STATUS_MAP.get(status)
        if mapped is None:
            return
        for st in entry["stages"]:
            if st["name"] == stage:
                st["status"] = mapped
        entry["status"] = "running"
        running = [st["name"] for st in entry["stages"] if st["status"] == "running"]
        entry["current_stage"] = running[-1] if running else entry["current_stage"]

    return on_stage


def _finish_progress(entry: dict, failed: bool) -> None:
    for st in entry["stages"]:
        if st["status"] in ("pending", "running"):
            st["status"] = "error" if failed and st["status"] == "running" else "skipped"
    entry["status"] = "error" if failed else "done"
    entry["current_stage"] = None


def err(code: str, message: str, field: str | None = None, remediation: str | None = None):
    body: dict[str, Any] = {"code": code, "message": message}
    if field:
        body["field"] = field
    if remediation:
        body["remediation"] = remediation
    return body


@router.get("/health")
async def health() -> dict:
    llm = LLMClient()
    return {
        "status": "ok",
        "llm_provider": llm.provider,
        "fallback_chain": llm.chain,
        "mock_mode": llm.provider == "mock",
        "config_version": cfg("app.config_version"),
    }


@router.post("/screenings")
async def create_screening(
    jd_text: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    pasted_resumes: list[str] = Form(default=[]),
    github_usernames: list[str] = Form(default=[]),
    portfolio_urls: list[str] = Form(default=[]),
    linkedin_files: list[UploadFile] = File(default=[]),  # aligned by index with `files`
    consent_github: bool = Form(True),
    consent_linkedin: bool = Form(True),
    consent_portfolio: bool = Form(True),
):
    if not jd_text or not jd_text.strip():
        return JSONResponse(
            status_code=422,
            content=err(
                "JD_REQUIRED", "A job description is required.", "jd_text",
                "Paste the job description before screening.",
            ),
        )
    if not files and not pasted_resumes:
        return JSONResponse(
            status_code=422,
            content=err(
                "NO_RESUMES", "Upload at least one resume or paste one.", "files",
                "Add a PDF, DOCX or TXT file, or paste the resume text.",
            ),
        )

    consent = {"github": consent_github, "linkedin": consent_linkedin, "portfolio": consent_portfolio}
    sid = str(uuid.uuid4())[:8]
    inputs: list[dict] = []
    for i, f in enumerate(files):
        linkedin_export = None
        if i < len(linkedin_files) and linkedin_files[i] and linkedin_files[i].filename:
            content = (await linkedin_files[i].read()).decode("utf-8", "replace")
            # A .json upload is a structured export; anything else is treated as a
            # "Save to PDF" text export (Spec 11: no scraping, export only).
            import json as _json

            if (linkedin_files[i].filename or "").lower().endswith(".json"):
                try:
                    linkedin_export = {"type": "structured_json", "content": _json.loads(content)}
                except ValueError:
                    linkedin_export = {"type": "pdf_export", "content": content}
            else:
                linkedin_export = {"type": "pdf_export", "content": content}
        inputs.append(
            {
                "filename": f.filename,
                "data": await f.read(),
                "candidate_name": (f.filename or "").rsplit(".", 1)[0].replace("_", " ").title(),
                "github_username": github_usernames[i] if i < len(github_usernames) else None,
                "portfolio_url": portfolio_urls[i] if i < len(portfolio_urls) else None,
                "linkedin_export": linkedin_export,
                "consent": consent,
            }
        )
    for text in pasted_resumes:
        if text.strip():
            inputs.append(
                {
                    "pasted_text": text,
                    "candidate_name": text.strip().splitlines()[0][:60] or "Pasted candidate",
                    "consent": consent,
                }
            )

    get_store().create_screening(sid, len(inputs))
    progress = _register_progress(sid, inputs)
    asyncio.create_task(_run(sid, jd_text, inputs, progress))
    return {"screening_id": sid, "total_candidates": len(inputs), "status": "processing"}


async def _run(
    sid: str, jd_text: str, inputs: list[dict], progress: list[dict] | None = None
) -> None:
    store = get_store()
    done = 0
    progress = progress if progress is not None else _new_progress(inputs)
    for item, entry in zip(inputs, progress, strict=True):
        entry["status"] = "running"
        try:
            report = await screen_candidate(
                jd_text=jd_text, llm=LLMClient(), on_stage=_stage_callback(entry), **item
            )
        except Exception as exc:
            report = {
                "candidate_name": item.get("candidate_name", "Candidate"),
                "overall_match_score": 0,
                "recommendation": "Review Manually",
                "summary": f"This file could not be screened: {exc}",
                "requirement_match": [],
                "extensions": {"status": "Error", "error": str(exc),
                               "candidate_id": str(uuid.uuid4())[:8]},
            }
        _finish_progress(entry, failed=report["extensions"].get("status") == "Error")
        cid = report["extensions"]["candidate_id"]
        row = _row(cid, report)
        store.save_candidate(cid, sid, report, row)
        store.append_audit(
            cid,
            "screening_completed",
            {
                "screening_id": sid,
                "integrity_verdict": row["integrity_verdict"],
                "integrity_action": row["integrity_action"],
                "config_version": cfg("app.config_version"),
            },
        )
        done += 1
        store.update_screening(sid, status="processing", done=done)
    store.update_screening(sid, status="complete", done=done)


def _row(cid: str, r: dict) -> dict:
    ext = r.get("extensions", {})
    auth = ext.get("authenticity") or {}
    integ = ext.get("integrity") or {}
    return {
        "candidate_id": cid,
        "candidate_name": r["candidate_name"],
        "overall_match_score": r["overall_match_score"],
        "base_score": ext.get("score_breakdown", {}).get("base_score"),
        "score_confidence": ext.get("score_breakdown", {}).get("score_confidence"),
        "recommendation": r["recommendation"],
        "strengths": r.get("strengths", [])[:2],
        "gaps": r.get("missing_or_unclear_skills", [])[:3],
        "status": ext.get("status", "Complete"),
        "integrity_verdict": (integ.get("intent") or "benign"),
        "integrity_action": integ.get("recommended_action", "proceed"),
        "authenticity_band": auth.get("band"),
        "mock_mode": ext.get("meta", {}).get("mock_mode", True),
    }


@router.get("/screenings/{sid}")
async def get_screening(sid: str):
    screening = get_store().get_screening(sid)
    if screening is None:
        raise HTTPException(404, err("NOT_FOUND", f"No screening {sid}."))
    # `progress` is additive; every other key is exactly what the store returned.
    return {**screening, "progress": _PROGRESS.get(sid, [])}


@router.get("/candidates/{cid}")
async def get_candidate(cid: str):
    report = get_store().get_candidate(cid)
    if report is None:
        raise HTTPException(404, err("NOT_FOUND", f"No candidate {cid}."))
    return report


@router.post("/candidates/{cid}/decision")
async def record_decision(cid: str, decision: str = Form(...), note: str = Form("")):
    store = get_store()
    if store.get_candidate(cid) is None:
        raise HTTPException(404, err("NOT_FOUND", f"No candidate {cid}."))
    import datetime

    entry = {
        "candidate_id": cid,
        "decision": decision,
        "note": note,
        "at": datetime.datetime.now(datetime.UTC).isoformat(),
        "config_version": cfg("app.config_version"),
    }
    store.append_audit(
        cid,
        "recruiter_decision",
        {"decision": decision, "note": note, "at": entry["at"],
         "config_version": entry["config_version"]},
    )  # append-only
    return entry


@router.get("/candidates/{cid}/audit")
async def get_audit(cid: str):
    # Same shape as before: recruiter decisions only. screening_completed rows
    # stay in the table but are not returned here (frontend reads a.decision).
    return [
        {"candidate_id": cid, **a["payload"]}
        for a in get_store().list_audit(cid, event="recruiter_decision")
    ]


@router.post("/interview-questions")
async def interview_questions(payload: dict):
    return await generate_interview_questions(payload.get("requirements", []))


@router.get("/samples")
async def samples() -> list[dict]:
    """Seeded sample reports for the landing dashboard (not stored in the DB)."""
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(_SAMPLE_OUTPUT.glob("*.json"), key=lambda p: p.name)
    ]


@router.get("/samples/jd")
async def sample_jd() -> PlainTextResponse:
    if not _SAMPLE_JD.is_file():
        raise HTTPException(404, err("NOT_FOUND", "Sample job description is not available."))
    return PlainTextResponse(_SAMPLE_JD.read_text(encoding="utf-8"))


# --- Module-level endpoints (Spec 11 and 10.4) ------------------------------


class ResumeIn(BaseModel):
    raw_text: str = ""
    parsed_profile: dict | None = None


class LinkedInIn(BaseModel):
    type: str | None = None
    content: Any = None


class JobContextIn(BaseModel):
    required_skills: list[str] = []
    role_level: str | None = None


class ConsentIn(BaseModel):
    github: bool = True
    linkedin: bool = True
    portfolio: bool = True


class AssessIn(BaseModel):
    candidate_id: str = ""
    resume: ResumeIn = ResumeIn()
    github_username: str | None = None
    linkedin: LinkedInIn | None = None
    portfolio_url: str | None = None
    job_context: JobContextIn = JobContextIn()
    consent: ConsentIn = ConsentIn()


class SkillsAnalyzeIn(BaseModel):
    resume_text: str = ""
    required_skills: list[str] | None = None
    jd_text: str | None = None


def _bad(status: int, code: str, message: str, field: str | None, remediation: str | None):
    return HTTPException(status, err(code, message, field, remediation))


def _check_text(value: str | None, field: str, *, required: bool) -> None:
    if value is None or not value.strip():
        if required:
            raise _bad(
                422, "RESUME_REQUIRED", f"{field} is required and must not be empty.", field,
                "Provide the resume text to analyse.",
            )
        return
    max_bytes = int(cfg("ingest.max_bytes", 10 * 1024 * 1024))
    if len(value.encode("utf-8")) > max_bytes:
        raise _bad(
            413, "TEXT_TOO_LARGE",
            f"{field} is larger than the {max_bytes // (1024 * 1024)} MB limit.", field,
            "Shorten the text or split it into smaller requests.",
        )


def _redacted_profile(text: str) -> tuple[str, dict]:
    redacted = redact_for_scoring(text)
    return redacted, structure_resume(redacted)


@router.post("/authenticity/assess")
async def authenticity_assess(body: AssessIn):
    _check_text(body.resume.raw_text, "resume.raw_text", required=True)
    if body.linkedin and isinstance(body.linkedin.content, str):
        _check_text(body.linkedin.content, "linkedin.content", required=False)
    cid = body.candidate_id.strip() or str(uuid.uuid4())[:8]
    redacted = redact_for_scoring(body.resume.raw_text)
    parsed = body.resume.parsed_profile or structure_resume(redacted)
    linkedin = (
        {"type": body.linkedin.type, "content": body.linkedin.content}
        if body.linkedin and body.linkedin.content
        else None
    )
    report = await assess(
        cid,
        redacted,
        parsed,
        body.job_context.required_skills,
        consent=body.consent.model_dump(),
        sources={
            "github": body.github_username,
            "portfolio": body.portfolio_url,
            "linkedin": linkedin,
        },
        llm=LLMClient(),
    )
    get_store().put_authenticity_report(cid, report)
    return report


@router.get("/authenticity/{candidate_id}")
async def authenticity_get(candidate_id: str):
    store = get_store()
    report = store.get_authenticity_report(candidate_id)
    if report is None:
        screened = store.get_candidate(candidate_id)
        report = ((screened or {}).get("extensions") or {}).get("authenticity")
    if report is None:
        raise HTTPException(
            404, err("NOT_FOUND", f"No authenticity assessment for {candidate_id}.")
        )
    return report


@router.post("/skills/analyze")
async def skills_analyze(body: SkillsAnalyzeIn):
    _check_text(body.resume_text, "resume_text", required=True)
    _check_text(body.jd_text, "jd_text", required=False)
    if body.required_skills:
        skills = [s for s in body.required_skills if s and s.strip()]
    elif body.jd_text and body.jd_text.strip():
        skills = [
            r["requirement"]
            for r in extract_requirements(body.jd_text)
            if r.get("category") not in ("min experience", "education")
        ]
    else:
        raise _bad(
            422, "SKILLS_REQUIRED", "Provide required_skills or jd_text.", "required_skills",
            "Send a non-empty required_skills list, or the job description as jd_text.",
        )
    _, parsed = _redacted_profile(body.resume_text)
    return [analyze_skill(s, parsed) for s in skills]
