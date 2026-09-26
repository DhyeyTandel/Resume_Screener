"""Module B slice: claim extraction, scoring, bands, inflation signals (Spec 11).

Scope note (see ASSUMPTIONS.md): Stages 1, 5, 6 and 7 are implemented
deterministically. Live GitHub / LinkedIn / portfolio collectors (Stage 2) and
the LLM claim-evidence judge (Stage 3) are stubbed: without a collected source
every claim is UNVERIFIABLE, which by the spec's own rule lowers CONFIDENCE and
produces INSUFFICIENT_EVIDENCE - never a negative judgment of the candidate.
"""
from __future__ import annotations
import re

from ...config import cfg
from .collectors.github import GitHubEvidence, collect_github
from .collectors.linkedin import LinkedInEvidence, collect_linkedin
from .consistency import check_anachronisms, check_linkedin_consistency, check_role_overlap
from .matching import (STATUS_V, authenticity_flags, judge_project_claim, judge_role_claim,
                       judge_skill_claim)

BUZZWORDS = {
    "synergy", "rockstar", "ninja", "guru", "passionate", "dynamic", "results-driven",
    "self-starter", "go-getter", "thought leader", "world-class", "cutting-edge",
    "best-in-class", "visionary", "seamless", "robust", "leverage", "spearheaded",
    "revolutionary", "game-changing", "hardworking", "detail-oriented", "team player",
}
VAGUE_VERBS = {
    "helped", "assisted", "worked", "involved", "participated", "supported", "contributed",
    "handled", "managed", "responsible",
}
_METRIC = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent|x|k|m|million|users|requests|ms|seconds)\b", re.I)
_ANCHOR = re.compile(r"\b(from|to|over|within|across|by|per|reduc\w+|increas\w+|baseline)\b", re.I)


def extract_claims(resume_text: str, parsed: dict) -> list[dict]:
    """Stage 1: atomic claims with spans that must map to exact resume text."""
    claims: list[dict] = []

    def add(ctype: str, text: str, depth: str | None = None) -> None:
        text = text.strip()
        if len(text) < 3:
            return
        start = resume_text.find(text)
        if start == -1:  # Reject any claim whose span does not map to resume text.
            return
        claims.append(
            {
                "claim_id": f"C{len(claims) + 1}",
                "type": ctype,
                "text": text,
                "source_span": {"start": start, "end": start + len(text)},
                "entities": {
                    "tech": sorted({t for t in parsed["skills"] if t.lower() in text.lower()}),
                    "numbers": _METRIC.findall(text),
                },
                "specificity_score": specificity(text),
                **({"depth": depth} if depth else {}),
            }
        )

    for s in parsed["skills"]:
        used_in_project = any(s.lower() in p["description"].lower() for p in parsed["projects"])
        used_in_role = any(
            s.lower() in " ".join(e["relevant_points"]).lower() for e in parsed["experience"]
        )
        depth = (
            "USED_IN_ROLE" if used_in_role
            else "USED_IN_PROJECT" if used_in_project
            else "LISTED_ONLY"
        )
        add("SKILL", s, depth)
    for p in parsed["projects"]:
        add("PROJECT", p["description"] or p["name"])
    for e in parsed["experience"]:
        add("ROLE", f"{e['title']}".strip())
        for pt in e["relevant_points"]:
            add("METRIC" if _METRIC.search(pt) else "ACHIEVEMENT", pt)
    for ed in parsed["education"]:
        add("EDUCATION", ed)
    for c in parsed["certifications"]:
        add("CERTIFICATION", c)
    return claims


def specificity(text: str) -> float:
    words = re.findall(r"[a-z]+", text.lower())
    if not words:
        return 0.0
    vague = sum(1 for w in words if w in VAGUE_VERBS)
    concrete = len(_METRIC.findall(text)) + len(re.findall(r"\b[A-Z][\w.+#]{2,}\b", text))
    return round(max(0.0, min(1.0, 0.3 + 0.15 * concrete - 0.2 * vague)), 3)


def inflation_signals(resume_text: str, parsed: dict, claims: list[dict]) -> dict:
    """Stage 5. Text-level only, low weight, and display-only downstream."""
    words = re.findall(r"[a-z-]+", resume_text.lower())
    n = max(1, len(words))
    buzz = sum(1 for w in words if w in BUZZWORDS) / n
    specificities = [c["specificity_score"] for c in claims] or [0.5]
    specificity_gap = 1 - sum(specificities) / len(specificities)
    listed = [c for c in claims if c["type"] == "SKILL"]
    listed_only = [c for c in listed if c.get("depth") == "LISTED_ONLY"]
    bloat = len(listed_only) / len(listed) if listed else 0.0
    metrics = _METRIC.findall(resume_text)
    unanchored = sum(
        1 for line in resume_text.splitlines() if _METRIC.search(line) and not _ANCHOR.search(line)
    ) / max(1, len(metrics))
    openers = [ln.strip().split(" ")[0].lower() for ln in resume_text.splitlines() if ln.strip()]
    repetition = 1 - (len(set(openers)) / len(openers)) if openers else 0.0
    sub = {
        "buzzword_density": round(min(1.0, buzz * 40), 3),
        "specificity_gap": round(specificity_gap, 3),
        "skill_list_bloat": round(bloat, 3),
        "unanchored_metrics": round(min(1.0, unanchored), 3),
        "opener_repetition": round(repetition, 3),
    }
    index = round(sum(sub.values()) / len(sub), 3)
    return {"inflation_index": index, "sub_signals": sub}


async def assess(
    candidate_id: str,
    resume_text: str,
    parsed: dict,
    required_skills: list[str],
    consent: dict | None = None,
    sources: dict | None = None,
    github_fetch=None,
) -> dict:
    """Stages 1, 5, 6, 7 + live Stage 2/3 for GitHub. Sources absent => UNVERIFIABLE
    => confidence drops only, never a negative judgment (Spec 2.4)."""
    import os

    consent = consent or {}
    sources = sources or {}
    claims = extract_claims(resume_text, parsed)
    infl = inflation_signals(resume_text, parsed, claims)

    source_status = {}
    for name in ("github", "linkedin", "portfolio"):
        if consent.get(name) is False:
            source_status[name] = "no_consent"
        elif sources.get(name):
            source_status[name] = "ok"
        else:
            source_status[name] = "missing"

    gh = GitHubEvidence(username="", status="missing")
    if source_status["github"] == "ok":
        gh = await collect_github(
            sources.get("github"),
            token=os.getenv(cfg("authenticity.github.token_env", "GITHUB_TOKEN")),
            fetch=github_fetch,
        )
        if gh.status == "error":
            source_status["github"] = "error"

    li = LinkedInEvidence(status="missing")
    if source_status["linkedin"] == "ok":
        li = collect_linkedin(sources.get("linkedin"))
        if li.status == "error":
            source_status["linkedin"] = "error"

    n_sources = sum(1 for v in source_status.values() if v == "ok")

    cw = cfg("authenticity.claim_weights")
    mult = float(cfg("authenticity.weights.required_skill_multiplier", 1.5))
    req_lower = {s.lower() for s in required_skills}
    total_w = verifiable_w = weighted_v_sum = judge_conf_sum = 0.0
    out_claims = []
    for c in claims:
        w = float(cw.get(c["type"], 1.0))
        required = c["type"] == "SKILL" and c["text"].lower() in req_lower
        if required:
            w *= mult
        total_w += w

        if c["type"] == "SKILL":
            judged = judge_skill_claim(c["text"], gh, required)
        elif c["type"] == "PROJECT":
            judged = judge_project_claim(c["text"], gh)
        elif c["type"] == "ROLE":
            judged = judge_role_claim(c["text"], li)
        elif c["type"] == "METRIC":
            # Spec 11 Stage 3: METRIC is VERIFIED only with a code/artifact trace,
            # never UNSUPPORTED by default without one.
            judged = {"status": "UNVERIFIABLE", "evidence": [],
                      "rationale": "No code artifact could confirm this metric.",
                      "judge_confidence": 0.0}
        else:
            judged = {"status": "UNVERIFIABLE", "evidence": [],
                      "rationale": "No accessible source could confirm this claim.",
                      "judge_confidence": 0.0}

        status = judged["status"]
        epistemic = "EVIDENCE" if status in ("VERIFIED", "CORROBORATED") else (
            "INFERENCE" if status == "WEAK" else "UNKNOWN"
        )
        v = STATUS_V.get(status)
        if v is not None:
            verifiable_w += w
            weighted_v_sum += w * v
            judge_conf_sum += judged["judge_confidence"]
        out_claims.append(
            {
                "claim_id": c["claim_id"], "type": c["type"], "text": c["text"],
                "status": status, "epistemic_tag": epistemic,
                "evidence": judged["evidence"], "rationale": judged["rationale"],
                "judge_confidence": judged["judge_confidence"], "weight": round(w, 3),
            }
        )

    consistency_findings = (
        check_anachronisms(resume_text)
        + check_role_overlap(parsed["experience"])
        + check_linkedin_consistency(parsed["experience"], li)
    )
    contradictions = [
        {"type": c["type"], "detail": c["detail"], "sources": c["sources"]}
        for c in consistency_findings
    ]
    checks_performed = max(
        1,
        len(re.findall(r"\d+\s*\+?\s*years?", resume_text, re.I))
        + max(0, len(parsed["experience"]) - 1)  # pairs of roles checked for overlap
        + (len(parsed["experience"]) if li.status == "ok" else 0),
    )
    consistency = round(1 - len(contradictions) / checks_performed, 3)

    coverage = round(verifiable_w / total_w, 3) if total_w else 0.0
    reliability = round((weighted_v_sum / verifiable_w + 1) / 2, 3) if verifiable_w else 0.5
    mean_judge_conf = round(judge_conf_sum / len(out_claims), 3) if out_claims else 0.0
    confidence = round(
        0.5 * coverage + 0.3 * min(n_sources / 3, 1) + 0.2 * mean_judge_conf, 3
    )
    w = cfg("authenticity.weights")
    authenticity = round(
        w["alpha"] * reliability + w["beta"] * consistency + w["gamma"] * (1 - infl["inflation_index"]),
        3,
    )

    bands = cfg("authenticity.bands")
    required_contradiction = any(
        c["status"] == "CONTRADICTED" and c["type"] == "SKILL" and c["text"].lower() in req_lower
        for c in out_claims
    ) or bool(contradictions)  # anachronisms are resume-level; treat as a forcing signal too
    if confidence < float(bands["min_confidence"]):
        band = "INSUFFICIENT_EVIDENCE"
    elif required_contradiction:
        band = "NEEDS_VERIFICATION"  # Spec: a contradiction forces at least this band.
    elif confidence >= float(bands["high_trust"]):
        band = "HIGH_TRUST"
    elif confidence >= float(bands["moderate"]):
        band = "MODERATE"
    else:
        band = "NEEDS_VERIFICATION"

    _NEEDS_VERIFY = {"UNSUPPORTED", "WEAK", "UNVERIFIABLE", "CONTRADICTED"}
    gaps = [
        {
            "claim_id": c["claim_id"],
            "what_to_verify": f"Ask the candidate to describe their hands-on use of "
            f"{c['text'][:60]}.",
            "priority": "high" if c["type"] in ("SKILL", "PROJECT", "ROLE") else "med",
        }
        for c in out_claims
        if c["type"] == "SKILL" and c["text"].lower() in req_lower
        and c["status"] in _NEEDS_VERIFY
    ][:10]

    return {
        "candidate_id": candidate_id,
        "band": band,
        "scores": {
            "authenticity": authenticity,
            "reliability": reliability,
            "consistency": consistency,
            "coverage": coverage,
            "inflation_index": infl["inflation_index"],
            "assessment_confidence": confidence,
        },
        "inflation_sub_signals": infl["sub_signals"],
        "sources_used": source_status,
        "skill_evidence": [
            {
                "skill": c["text"],
                "required": c["text"].lower() in req_lower,
                "status": c["status"],
                "evidence": c["evidence"],
            }
            for c in out_claims
            if c["type"] == "SKILL"
        ],
        "claims": out_claims,
        "contradictions": contradictions,
        "authenticity_flags": authenticity_flags(gh),
        "verification_gaps": gaps,
        "recruiter_summary": _recruiter_summary(
            gh, out_claims, confidence, n_sources
        ),
        "meta": {
            "model": "deterministic-v1",
            "config_version": cfg("app.config_version"),
            "latency_ms": 0,
            "llm_calls": 0,
        },
    }


def _recruiter_summary(gh: GitHubEvidence, claims: list[dict], confidence: float, n_sources: int) -> str:
    if n_sources == 0:
        return (
            f"No external source was available for this candidate, so {len(claims)} resume "
            "claims could not be checked against code, an employment record or a portfolio. "
            "That is common and is not a negative signal: private work, NDAs and a simple "
            "absence of a public profile all produce this result. The assessment confidence is "
            f"{confidence}, so no headline trust score is shown. Verify the listed items in "
            "interview rather than treating them as doubtful."
        )
    verified = sum(1 for c in claims if c["status"] == "VERIFIED")
    unsupported = sum(1 for c in claims if c["status"] == "UNSUPPORTED")
    bits = [f"GitHub evidence was reviewed across {len(gh.repos)} repositories."]
    if verified:
        bits.append(f"{verified} claim(s) have direct code or manifest evidence.")
    if unsupported:
        bits.append(
            f"{unsupported} claim(s) have no supporting evidence in the sources checked, "
            "which is worth a direct question in interview rather than an assumption either way."
        )
    bits.append(f"Overall assessment confidence is {confidence}.")
    return " ".join(bits)
