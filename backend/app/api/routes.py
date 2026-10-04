"""FastAPI surface (Spec 14.1)."""
from __future__ import annotations
import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ..config import cfg
from ..llm.client import LLMClient
from ..modules.interview_questions.generator import generate_interview_questions
from ..db.store import get_store
from ..pipeline.orchestrator import screen_candidate

router = APIRouter(prefix="/v1")


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
    asyncio.create_task(_run(sid, jd_text, inputs))
    return {"screening_id": sid, "total_candidates": len(inputs), "status": "processing"}


async def _run(sid: str, jd_text: str, inputs: list[dict]) -> None:
    store = get_store()
    done = 0
    for item in inputs:
        try:
            report = await screen_candidate(jd_text=jd_text, llm=LLMClient(), **item)
        except Exception as exc:  # noqa: BLE001 - one bad file never stops the batch
            report = {
                "candidate_name": item.get("candidate_name", "Candidate"),
                "overall_match_score": 0,
                "recommendation": "Review Manually",
                "summary": f"This file could not be screened: {exc}",
                "requirement_match": [],
                "extensions": {"status": "Error", "error": str(exc),
                               "candidate_id": str(uuid.uuid4())[:8]},
            }
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
    return screening


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
