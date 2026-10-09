"""FastAPI surface (Spec 14.1)."""
from __future__ import annotations

import asyncio
import json
import os
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..config import cfg
from ..db.store import get_store
from ..llm.client import LLMClient
from ..llm.redaction import redact_for_scoring
from ..modules.authenticity_engine.engine import assess
from ..modules.core_screening.core import extract_requirements, structure_resume
from ..modules.interview_questions.generator import generate_interview_questions
from ..modules.skill_intelligence.transfer import analyze_skill
from ..pipeline.orchestrator import UI_STAGES, screen_candidate
from .auth import auth_required, can_access
from .limits import release_admission, screening_slot, screening_slots, try_admit

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


# --- Ownership guards (A-30) -------------------------------------------------
# Every route with an id in its path declares exactly one of these as a dependency. They all go
# through can_access, and a refusal is the SAME 404 body as a missing id, never 403, so a
# recruiter cannot probe whether another recruiter's id exists. With auth off they return before
# touching the store. tests/test_ownership.py walks app.routes and fails if an id route lacks one.
def _actor(request: Request) -> str | None:
    return getattr(request.state, "actor", None)


def _not_found(what: str, key: str) -> HTTPException:
    return HTTPException(404, err("NOT_FOUND", f"No {what} {key}."))


def _guard_screening(request: Request, sid: str) -> None:
    if not auth_required():
        return
    found, owner = get_store().owner_of("screenings", sid)
    if found and not can_access(owner, _actor(request)):
        raise _not_found("screening", sid)


def _guard_candidate(request: Request, cid: str) -> None:
    if not auth_required():
        return
    found, owner = get_store().owner_of("candidates", cid)
    if found and not can_access(owner, _actor(request)):
        raise _not_found("candidate", cid)


def _guard_audit(request: Request, cid: str) -> None:
    """Audit is keyed by candidate id and outlives the candidate row, so after erasure it is 404
    for everyone. With auth on, a non-admin also needs a live candidate row they own."""
    store = get_store()
    if store.was_erased(cid):
        raise _not_found("candidate", cid)
    if not auth_required():
        return
    actor = _actor(request)
    found, owner = store.owner_of("candidates", cid)
    if not can_access(owner if found else None, actor):
        raise _not_found("candidate", cid)


def _guard_authenticity(request: Request, candidate_id: str) -> None:
    if not auth_required():
        return
    found, owner = get_store().authenticity_owner(candidate_id)
    if found and not can_access(owner, _actor(request)):
        raise HTTPException(
            404, err("NOT_FOUND", f"No authenticity assessment for {candidate_id}.")
        )


OWNERSHIP_GUARDS = frozenset(
    {_guard_screening, _guard_candidate, _guard_audit, _guard_authenticity}
)


def _key_present(provider: str) -> bool:
    env = cfg(f"llm.{provider}.api_key_env", "")
    return bool(env and os.getenv(str(env)))


@router.get("/health")
async def health() -> dict:
    llm = LLMClient()
    return {
        "status": "ok",
        "llm_provider": llm.provider,
        "configured_provider": llm.provider,
        # Presence only, never the key. The chain's mock tail is always keyless.
        "key_present": _key_present(llm.provider),
        "fallback_chain": llm.chain,
        "mock_mode": llm.provider == "mock",
        "config_version": cfg("app.config_version"),
        # A flag only: never keys or labels. The SPA reads it to decide whether to show a prompt.
        "auth_required": auth_required(),
    }


def _lim(name: str, default: int) -> int:
    return int(cfg("ingest." + name, default))


def sanitize_filename(name: str | None) -> str:
    """A safe display name for an upload: no directories, no control or invisible characters,
    bounded length, extension kept (the loader dispatches on it)."""
    text = name or ""
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn"))
    text = " ".join(text.split()).lstrip(".").strip()
    if not text:
        return "upload"
    limit = max(_lim("max_filename_chars", 120), 8)
    if len(text) > limit:
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            text = stem[: limit - len(ext) - 1] + "." + ext
        else:
            text = text[:limit]
    return text


def _clean_label(text: str, n: int = 60) -> str:
    return "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf")).strip()[:n]


def _limit_error(status: int, code: str, message: str, field: str, remediation: str):
    return JSONResponse(status_code=status, content=err(code, message, field, remediation))


def _linkedin_export_from_upload(filename: str, raw: bytes) -> dict:
    """A .json upload is a structured export. A .pdf ("Save to PDF") goes through the sandboxed
    PDF loader to get its text; decoding the bytes as UTF-8 turned a real export into garbage
    and a silent "no roles". An unreadable PDF becomes an explicit unreadable_export so the
    source reads "error" (Spec 11 Stage 2: malformed PDF), never a crash. Anything else is
    treated as pasted export text. Export only, never scraping (Spec 2.9)."""
    import json as _json

    low = filename.lower()
    if low.endswith(".pdf") or raw[:5] == b"%PDF-":
        from ..parsing.loader import UnreadableFile, load

        try:
            text = load(filename if low.endswith(".pdf") else "linkedin.pdf", raw).visible_text
        except UnreadableFile as exc:
            return {"type": "unreadable_export", "content": exc.reason}
        except Exception:
            return {"type": "unreadable_export", "content": "The PDF could not be read."}
        return {"type": "pdf_export", "content": text}
    content = raw.decode("utf-8", "replace")
    if low.endswith(".json"):
        try:
            return {"type": "structured_json", "content": _json.loads(content)}
        except (ValueError, RecursionError):
            pass
    return {"type": "pdf_export", "content": content}


async def _bounded_read(f: UploadFile, limit: int) -> bytes:
    """At most limit+1 bytes, so callers can tell "over the limit" without holding the rest."""
    return await f.read(limit + 1)


def _check_screening_limits(
    jd_text: str, files: list, pasted: list[str], linkedin: list, github: list, portfolio: list
):
    max_files, max_pasted = _lim("max_files", 25), _lim("max_pasted", 25)
    jd_max = _lim("jd_max_chars", 50000)
    if len(jd_text) > jd_max:
        return _limit_error(
            413, "JD_TOO_LARGE", f"The job description is longer than {jd_max:,} characters.",
            "jd_text", "Paste only the role description, not the whole careers page.",
        )
    if len(files) > max_files:
        return _limit_error(
            422, "TOO_MANY_FILES", f"At most {max_files} resume files can be screened at once.",
            "files", f"Split the batch into groups of {max_files} or fewer.",
        )
    if len(pasted) > max_pasted:
        return _limit_error(
            422, "TOO_MANY_PASTED_RESUMES",
            f"At most {max_pasted} pasted resumes can be screened at once.", "pasted_resumes",
            f"Split the batch into groups of {max_pasted} or fewer.",
        )
    if len(linkedin) > max_files or len(github) > max_files or len(portfolio) > max_files:
        return _limit_error(
            422, "TOO_MANY_ATTACHMENTS",
            f"At most {max_files} LinkedIn, GitHub or portfolio entries can be sent at once.",
            "linkedin_files", "Send one entry per resume file.",
        )
    pasted_max = _lim("pasted_max_chars", 200000)
    for text in pasted:
        if len(text) > pasted_max:
            return _limit_error(
                413, "PASTED_RESUME_TOO_LARGE",
                f"A pasted resume is longer than {pasted_max:,} characters.", "pasted_resumes",
                "Paste only the resume text.",
            )
    return None


@router.post("/screenings")
async def create_screening(
    request: Request,
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
    too_big = _check_screening_limits(
        jd_text, files, pasted_resumes, linkedin_files, github_usernames, portfolio_urls
    )
    if too_big is not None:
        return too_big
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
    li_max = _lim("linkedin_max_bytes", 2 * 1024 * 1024)
    file_max = int(cfg("ingest.max_bytes", 10 * 1024 * 1024))
    for i, f in enumerate(files):
        linkedin_export = None
        if i < len(linkedin_files) and linkedin_files[i] and linkedin_files[i].filename:
            raw = await _bounded_read(linkedin_files[i], li_max)
            if len(raw) > li_max:
                return _limit_error(
                    413, "LINKEDIN_FILE_TOO_LARGE",
                    f"A LinkedIn export is larger than the {li_max / (1024 * 1024):g} MB limit.",
                    "linkedin_files", "Upload the profile export only, not other documents.",
                )
            linkedin_export = _linkedin_export_from_upload(linkedin_files[i].filename or "", raw)
        safe_name = sanitize_filename(f.filename)
        inputs.append(
            {
                "filename": safe_name,
                # One byte past the limit is enough for the loader to refuse it as before.
                "data": await _bounded_read(f, file_max),
                "candidate_name": safe_name.rsplit(".", 1)[0].replace("_", " ").title(),
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
                    "candidate_name": _clean_label(text.strip().splitlines()[0]) or "Pasted candidate",
                    "consent": consent,
                }
            )

    # Bounded queue: refuse up front (nothing stored yet) rather than queue without limit.
    if not try_admit(len(inputs)):
        return screening_slots.rejection_response()
    actor = getattr(request.state, "actor", None)
    get_store().create_screening(sid, len(inputs), owner=actor)
    progress = _register_progress(sid, inputs)
    # `actor` is passed only when auth is on, so the open-mode call shape is unchanged.
    asyncio.create_task(
        _run(sid, jd_text, inputs, progress, **({"actor": actor} if actor else {}))
    )
    return {"screening_id": sid, "total_candidates": len(inputs), "status": "processing"}


async def _run(
    sid: str,
    jd_text: str,
    inputs: list[dict],
    progress: list[dict] | None = None,
    actor: str | None = None,
) -> None:
    store = get_store()
    done = 0
    progress = progress if progress is not None else _new_progress(inputs)
    try:
        for item, entry in zip(inputs, progress, strict=True):
            entry["status"] = "running"
            try:
                async with screening_slot():  # at most N candidates screen at once, process-wide
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
            store.save_candidate(cid, sid, report, row, owner=actor)
            store.append_audit(
                cid,
                "screening_completed",
                {
                    "screening_id": sid,
                    "integrity_verdict": row["integrity_verdict"],
                    "integrity_action": row["integrity_action"],
                    "config_version": cfg("app.config_version"),
                    **({"actor": actor} if actor else {}),
                },
            )
            done += 1
            store.update_screening(sid, status="processing", done=done)
        store.update_screening(sid, status="complete", done=done)
    finally:
        release_admission(len(inputs))  # always, even if the run is cancelled or crashes


def _scrub_progress_names() -> None:
    """The in-memory progress list holds display names and is indexed by position, not id.
    After an erasure, blank any finished entry whose name no longer matches a stored row."""
    store = get_store()
    for sid, entries in _PROGRESS.items():
        screening = store.get_screening(sid)
        live = {c["candidate_name"] for c in (screening or {}).get("candidates", [])}
        for e in entries:
            if e["status"] in ("done", "error") and e["candidate_name"] not in live:
                e["candidate_name"] = "Erased candidate"


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
        # Compact per-task "which model wrote this", so the dashboard needs no extra fetch
        # of every full report just to label each row. Reasons are already redacted.
        "provenance": ext.get("meta", {}).get("provenance"),
    }


@router.get("/screenings/{sid}", dependencies=[Depends(_guard_screening)])
async def get_screening(sid: str):
    screening = get_store().get_screening(sid)
    if screening is None:
        raise HTTPException(404, err("NOT_FOUND", f"No screening {sid}."))
    # `progress` is additive; every other key is exactly what the store returned.
    return {**screening, "progress": _PROGRESS.get(sid, [])}


@router.get("/candidates/{cid}", dependencies=[Depends(_guard_candidate)])
async def get_candidate(cid: str):
    report = get_store().get_candidate(cid)
    if report is None:
        raise HTTPException(404, err("NOT_FOUND", f"No candidate {cid}."))
    return report


@router.post("/candidates/{cid}/decision", dependencies=[Depends(_guard_candidate)])
async def record_decision(
    request: Request, cid: str, decision: str = Form(...), note: str = Form("")
):
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
    # The note text goes to the erasable `notes` table; the audit entry keeps note_present only.
    payload = {"decision": decision, "at": entry["at"],
               "config_version": entry["config_version"]}
    # The authenticated key's LABEL (never the key). Absent when auth is off, so open-mode
    # entries are byte-for-byte what they were before.
    actor = getattr(request.state, "actor", None)
    if actor:
        payload["actor"] = entry["actor"] = actor
    store.record_decision(cid, payload, note)  # append-only audit row + erasable note
    return entry


@router.get("/candidates/{cid}/audit", dependencies=[Depends(_guard_audit)])
async def get_audit(cid: str):
    # Same shape as before: recruiter decisions only. screening_completed rows
    # stay in the table but are not returned here (frontend reads a.decision).
    return [{"candidate_id": cid, **p} for p in get_store().decisions(cid)]


@router.delete("/candidates/{cid}", dependencies=[Depends(_guard_candidate)])
async def erase_candidate(request: Request, cid: str):
    """Right to be forgotten. Deletes the report, row, authenticity report and recruiter notes.
    The append-only audit log keeps its entries (ids, verdicts, decisions, no free text) and
    gains one `erased` event with counts and the actor label, no personal data."""
    counts = get_store().erase_candidate(cid, actor=_actor(request))
    if counts is None:
        raise _not_found("candidate", cid)
    _scrub_progress_names()
    return {"erased": counts}


@router.delete("/screenings/{sid}", dependencies=[Depends(_guard_screening)])
async def erase_screening(request: Request, sid: str):
    store = get_store()
    screening = store.get_screening(sid)
    if screening is None:
        raise _not_found("screening", sid)
    if screening["status"] == "processing":
        # Deleting under a running screening would let it re-create candidates afterwards.
        return JSONResponse(
            status_code=409,
            content=err(
                "SCREENING_IN_PROGRESS", "This screening is still running.", None,
                "Wait for it to finish, then delete it.",
            ),
        )
    counts = store.erase_screening(sid, actor=_actor(request))
    if counts is None:
        raise _not_found("screening", sid)
    _PROGRESS.pop(sid, None)  # progress holds candidate names
    return {"erased": counts}


class InterviewReqIn(BaseModel):
    """One Module D input item (MASTER_SPEC Section 12)."""

    skill: str = Field(min_length=1, max_length=200)
    evidence_level: Literal["strong_evidence", "claimed", "not_demonstrated", "transferable"]
    detail: str | None = Field(default=None, max_length=2000)
    jd_priority: Literal["must_have", "nice_to_have"] | None = None


class InterviewQuestionsIn(BaseModel):
    requirements: list[InterviewReqIn] = Field(default_factory=list, max_length=100)


@router.post("/interview-questions")
async def interview_questions(body: InterviewQuestionsIn):
    return await generate_interview_questions(
        [r.model_dump(exclude_unset=True) for r in body.requirements]
    )


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
async def authenticity_assess(request: Request, body: AssessIn):
    _check_text(body.resume.raw_text, "resume.raw_text", required=True)
    if body.linkedin and isinstance(body.linkedin.content, str):
        _check_text(body.linkedin.content, "linkedin.content", required=False)
    cid = body.candidate_id.strip() or str(uuid.uuid4())[:8]
    if auth_required():
        # A supplied id must not overwrite another recruiter's report (INSERT OR REPLACE), so an
        # id that is taken by someone else is replaced by a fresh one rather than refused.
        found, owner = get_store().authenticity_owner(cid)
        if found and not can_access(owner, _actor(request)):
            cid = str(uuid.uuid4())[:8]
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
    get_store().put_authenticity_report(cid, report, owner=_actor(request))
    return report


@router.get("/authenticity/{candidate_id}", dependencies=[Depends(_guard_authenticity)])
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
