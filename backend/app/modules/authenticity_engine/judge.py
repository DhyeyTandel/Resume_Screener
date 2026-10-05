"""Module B Stage 3 LLM judge, layered on the deterministic rubric (Spec 11).

The deterministic judges in matching.py are the floor and the authority. The
model may only UPGRADE a WEAK or UNSUPPORTED claim, and only inside caps this
module enforces in code, whatever the model says:

  (a) evidence whose citation is not in the evidence index is dropped
  (b) status is capped by source type (LinkedIn <= CORROBORATED, portfolio <= WEAK,
      VERIFIED needs a GitHub citation with authorship_ratio >= threshold whose own
      collected metadata shows the claim - an unrelated authored repo caps at WEAK;
      METRIC is VERIFIED only with that code citation, else UNVERIFIABLE)
  (c) never below the deterministic status
  (d) if every evidence item is dropped, the deterministic result stands
  (e) rationale truncated to 2 sentences
  (f) judge_confidence clamped to [0, 1]
  (g) a status outside the rubric keeps the deterministic result

Evidence index: {citation: {"source": "github"|"linkedin"|"portfolio",
"authorship_ratio": float|None, "summary": str, "untrusted_text": str}}.
All third-party text goes to the model wrapped in untrusted_document tags.
"""
from __future__ import annotations

import asyncio
import re

from ...config import cfg
from ...llm.client import LLMClient
from ...llm.prompts_common import UNTRUSTED_DATA_RULE, wrap_untrusted
from .matching import _repo_covers_skill, project_matches_repo

RANK = {"UNSUPPORTED": 0, "WEAK": 1, "CORROBORATED": 2, "VERIFIED": 3}
RUBRIC_STATUSES = {"VERIFIED", "CORROBORATED", "WEAK", "UNSUPPORTED", "CONTRADICTED", "UNVERIFIABLE"}
JUDGE_CANDIDATE_STATUSES = {"WEAK", "UNSUPPORTED"}
_EXCERPT = 1500

SYSTEM_PROMPT = (
    UNTRUSTED_DATA_RULE
    + "\n\nYou judge whether collected evidence supports one resume claim. Rubric: VERIFIED "
    "(direct code, commits or manifest evidence from a repository the candidate authored), "
    "CORROBORATED (an independent second source agrees), WEAK (tangential evidence only), "
    "UNSUPPORTED (sources exist but show nothing). You may only propose a status at or above "
    "the deterministic_result status. Cite only citation ids listed in evidence_index; any "
    "other citation is discarded. Return JSON: {\"status\": str, \"evidence\": [{\"citation\": "
    "str, \"note\": str}], \"rationale\": \"at most 2 sentences\", \"judge_confidence\": 0..1}."
)


def _min_authorship() -> float:
    return float(cfg("authenticity.github.authorship_ratio_min", 0.30))


def _two_sentences(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", str(text).strip())
    return " ".join(parts[:2]).strip()


def _clamp01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _grounded(entry: dict, claim_type: str, claim_text: str) -> bool:
    """Does the cited repo's own collected metadata actually show this claim?
    Without this, the model could cite any authored repo, even an unrelated one,
    and launder an unsupported claim into VERIFIED."""
    if claim_type not in ("SKILL", "PROJECT"):
        return True  # METRIC and others: authorship is the bar the spec sets
    repo = entry.get("_repo")
    if repo is None:
        return False
    if claim_type == "SKILL":
        return _repo_covers_skill(repo, claim_text)
    # PROJECT: the same strict correspondence the deterministic judge uses, so the model
    # cannot ground a project on a repo that merely shares a generic word with it.
    return project_matches_repo(repo, claim_text)


def _cap_rank(citations: list[str], index: dict, claim_type: str,
              claim_text: str = "") -> tuple[int, bool]:
    """Highest status rank the cited sources allow, and whether a qualifying
    GitHub (authored repo that actually shows the claim) citation is present."""
    min_auth = _min_authorship()
    cap, qualifying = 0, False
    for cit in citations:
        entry = index[cit]
        src = entry.get("source")
        if src == "github":
            if (float(entry.get("authorship_ratio") or 0.0) >= min_auth
                    and _grounded(entry, claim_type, claim_text)):
                qualifying = True
                cap = max(cap, RANK["VERIFIED"])
            else:
                cap = max(cap, RANK["WEAK"])
        elif src == "linkedin":
            cap = max(cap, RANK["CORROBORATED"])
        elif src == "portfolio":
            cap = max(cap, RANK["WEAK"])
    return cap, qualifying


def _model_view(index: dict) -> list[dict]:
    return [
        {
            "citation": cit,
            "source": e.get("source"),
            "authorship_ratio": e.get("authorship_ratio"),
            "summary": e.get("summary", ""),
            "untrusted_text": wrap_untrusted(e.get("untrusted_text", "") or ""),
        }
        for cit, e in index.items()
    ]


def _validate(claim: dict, det: dict, raw: dict, index: dict) -> dict | None:
    """Apply post-checks. Returns an upgraded result or None to keep deterministic."""
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    if status not in RUBRIC_STATUSES:  # (g)
        return None
    if status not in RANK or RANK[status] <= RANK.get(det["status"], 0):  # (c) upgrade only
        return None
    items = raw.get("evidence")
    kept, seen = [], set()
    for it in items if isinstance(items, list) else []:
        cit = it.get("citation") if isinstance(it, dict) else None
        if isinstance(cit, str) and cit in index and cit not in seen:  # (a)
            seen.add(cit)
            kept.append({"source": index[cit]["source"], "citation": cit,
                         "note": str(it.get("note", ""))[:300]})
    if not kept:  # (d)
        return None
    cap, qualifying = _cap_rank(
        [e["citation"] for e in kept] + [e["citation"] for e in det["evidence"]
                                         if e.get("citation") in index], index, claim["type"], claim.get("text", ""))
    final_rank = min(RANK[status], cap)  # (b)
    if claim["type"] == "METRIC":
        # VERIFIED only with a code citation; otherwise UNVERIFIABLE, never UNSUPPORTED.
        if not qualifying or RANK[status] < RANK["VERIFIED"]:
            return None
        final_rank = RANK["VERIFIED"]
    if final_rank <= RANK.get(det["status"], 0):
        return None
    final = next(s for s, r in RANK.items() if r == final_rank)
    merged = list(det["evidence"]) + [e for e in kept
                                      if e["citation"] not in {d.get("citation") for d in det["evidence"]}]
    return {
        "status": final,
        "evidence": merged,
        "rationale": _two_sentences(raw.get("rationale", "")) or det["rationale"],  # (e)
        "judge_confidence": _clamp01(raw.get("judge_confidence")),  # (f)
    }


def _judge_concurrency() -> int:
    try:
        return max(1, int(cfg("authenticity.judge_concurrency", 4)))
    except (TypeError, ValueError):
        return 4


async def llm_judge_claims(claims: list[dict], evidence_index: dict, llm: LLMClient) -> list[dict]:
    """Judge each claim; `claims` items are {claim_id, type, text, deterministic: {...}}.

    Returns one dict per input claim, in input order: {claim_id, status, evidence, rationale,
    judge_confidence, llm_called, upgraded}. No LLM call when the index is empty or
    the claim is not WEAK/UNSUPPORTED, so output without sources is unchanged.

    The per-claim model calls run concurrently, at most `authenticity.judge_concurrency` at a
    time. Each claim is judged independently from its own deterministic result, so completion
    order cannot change any outcome, and gather returns results in claim order."""
    view = _model_view(evidence_index) if evidence_index else []
    sem = asyncio.Semaphore(_judge_concurrency())

    async def one(c: dict) -> dict:
        det = c["deterministic"]
        res = {"claim_id": c["claim_id"], "status": det["status"], "evidence": det["evidence"],
               "rationale": det["rationale"], "judge_confidence": det["judge_confidence"],
               "llm_called": False, "upgraded": False}
        if not evidence_index or det["status"] not in JUDGE_CANDIDATE_STATUSES:
            return res
        payload = {
            "claim": {"claim_id": c["claim_id"], "type": c["type"],
                      "text": wrap_untrusted(c["text"])},
            "deterministic": {"status": det["status"], "evidence": det["evidence"],
                              "rationale": det["rationale"],
                              "judge_confidence": det["judge_confidence"]},
            "evidence_index": view,
        }
        res["llm_called"] = True
        try:
            async with sem:
                r = await llm.complete_json(SYSTEM_PROMPT, payload, task="claim_judge", temperature=0)
            upgraded = _validate(c, det, r.data, evidence_index)
        except Exception:
            upgraded = None  # provider failure keeps the deterministic result
        if upgraded:
            res.update(upgraded)
            res["upgraded"] = True
        return res

    return list(await asyncio.gather(*(one(c) for c in claims)))
