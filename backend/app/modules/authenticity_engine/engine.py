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
from .collectors.portfolio import PortfolioEvidence, collect_portfolio
from .consistency import (
    _parse_year,
    check_anachronisms,
    check_graduation_consistency,
    check_linkedin_consistency,
    check_role_overlap,
    graduation_year,
)
from .judge import JUDGE_CANDIDATE_STATUSES, llm_judge_claims
from .matching import (
    STATUS_V,
    authenticity_flags,
    judge_project_claim,
    judge_role_claim,
    judge_skill_claim_with_portfolio,
    portfolio_flags,
)

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

    def add(ctype: str, text: str, depth: str | None = None, company: str | None = None) -> None:
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
                **({"company": company} if company else {}),
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
        add("ROLE", f"{e['title']}".strip(), company=e.get("company") or None)
        for pt in e["relevant_points"]:
            add("METRIC" if _METRIC.search(pt) else "ACHIEVEMENT", pt)
    for a in parsed.get("achievements", []):
        # The standalone Achievements section (Spec 11 Stage 1); same typing rule as bullets.
        add("METRIC" if _METRIC.search(a) else "ACHIEVEMENT", a)
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
    portfolio_fetch=None,
    llm=None,
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

    source_details: dict[str, dict] = {}
    gh = GitHubEvidence(username="", status="missing")
    if source_status["github"] == "ok":
        gh = await collect_github(
            sources.get("github"),
            token=os.getenv(cfg("authenticity.github.token_env", "GITHUB_TOKEN")),
            fetch=github_fetch,
            skills=[c["text"] for c in claims if c["type"] == "SKILL"] + list(required_skills),
        )
        if gh.status in ("error", "partial"):
            # The spec enum is ok|missing|no_consent|error: "partial" folds into "error";
            # the detail stays in source_details (and the recruiter summary).
            source_status["github"] = "error"
            source_details["github"] = {
                "status": gh.status,
                "reason": gh.partial_reason or gh.error,
                "requests_made": gh.requests_made,
            }
        elif gh.status != "ok":
            # E.g. an account with no public repositories comes back as "missing": nothing
            # was checked, so it is not a source and is never counted as one (Spec 2.4).
            source_status["github"] = "missing" if gh.status == "missing" else "error"
            source_details["github"] = {
                "status": gh.status, "reason": gh.error, "requests_made": gh.requests_made,
            }

    li = LinkedInEvidence(status="missing")
    if source_status["linkedin"] == "ok":
        li = collect_linkedin(sources.get("linkedin"))
        if li.status == "error":
            source_status["linkedin"] = "error"

    pf = PortfolioEvidence(url="", status="missing")
    if source_status["portfolio"] == "ok":
        pf = await collect_portfolio(sources.get("portfolio"), fetch=portfolio_fetch)
        if pf.status != "ok":
            # "disallowed" (robots.txt) folds into "error" for sources_used - the spec's
            # enum is ok|missing|no_consent|error; the exact reason stays on pf.error.
            source_status["portfolio"] = "error"

    n_sources = sum(1 for v in source_status.values() if v == "ok")
    if gh.status == "partial" and any(r.analyzed for r in gh.repos):
        n_sources += 1  # partly checked still produced real evidence

    li_findings = check_linkedin_consistency(parsed["experience"], li)

    cw = cfg("authenticity.claim_weights")
    mult = float(cfg("authenticity.weights.required_skill_multiplier", 1.5))
    req_lower = {s.lower() for s in required_skills}
    total_w = verifiable_w = weighted_v_sum = judge_conf_sum = 0.0
    out_claims = []
    judged_by_id: dict[str, dict] = {}
    for c in claims:
        required = c["type"] == "SKILL" and c["text"].lower() in req_lower
        if c["type"] == "SKILL":
            judged = judge_skill_claim_with_portfolio(c["text"], gh, pf, required)
        elif c["type"] == "PROJECT":
            judged = judge_project_claim(c["text"], gh)
        elif c["type"] == "ROLE":
            role_company = c.get("company") or next(
                (e.get("company") for e in parsed["experience"] if e.get("title") == c["text"]), None
            )
            judged = judge_role_claim(c["text"], li, company=role_company)
            judged = _apply_linkedin_conflict(judged, c["text"], role_company, li_findings)
        elif c["type"] == "METRIC":
            # Spec 11 Stage 3: METRIC is VERIFIED only with a code/artifact trace,
            # never UNSUPPORTED by default without one.
            judged = judge_metric_claim(c["text"], gh)
        else:
            judged = {"status": "UNVERIFIABLE", "evidence": [],
                      "rationale": "No accessible source could confirm this claim.",
                      "judge_confidence": 0.0}
        judged_by_id[c["claim_id"]] = judged

    # Optional LLM judge over WEAK/UNSUPPORTED claims; deterministic rules stay the floor.
    llm_calls = 0
    evidence_index = _build_evidence_index(gh, li, pf)
    if llm is not None and llm.provider != "mock" and evidence_index:
        todo = [
            {"claim_id": c["claim_id"], "type": c["type"], "text": c["text"],
             "deterministic": judged_by_id[c["claim_id"]]}
            for c in claims if judged_by_id[c["claim_id"]]["status"] in JUDGE_CANDIDATE_STATUSES
        ]
        for r in await llm_judge_claims(todo, evidence_index, llm):
            llm_calls += int(r["llm_called"])
            if r["upgraded"]:
                judged_by_id[r["claim_id"]] = {k: r[k] for k in
                                               ("status", "evidence", "rationale", "judge_confidence")}

    for c in claims:
        judged = judged_by_id[c["claim_id"]]
        w = float(cw.get(c["type"], 1.0))
        if c["type"] == "SKILL" and c["text"].lower() in req_lower:
            w *= mult
        total_w += w
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

    # Resume-internal findings (no second source disagrees) vs findings that come from a
    # second source (LinkedIn). Both are shown and both lower the consistency score, but only
    # the cross-source kind can force the band (see the band block below).
    resume_only_findings = (
        check_anachronisms(resume_text)
        + check_role_overlap(parsed["experience"])
        + check_graduation_consistency(parsed.get("education", []), parsed["experience"])
    )
    consistency_findings = resume_only_findings + li_findings
    grad_check = int(
        graduation_year(parsed.get("education", [])) is not None
        and any(_parse_year(e.get("start", "")) is not None for e in parsed["experience"])
    )
    contradictions = [
        {
            "type": c["type"], "detail": c["detail"], "sources": c["sources"],
            # resume_only: nothing but the resume disagrees with itself. It is reported and
            # asked about, but is not a source-contradicted claim and does not set the band.
            "scope": "resume_only" if c in resume_only_findings else "cross_source",
        }
        for c in consistency_findings
    ]
    checks_performed = max(
        1,
        len(re.findall(r"\d+\s*\+?\s*years?", resume_text, re.I))
        + max(0, len(parsed["experience"]) - 1)  # pairs of roles checked for overlap
        + (len(parsed["experience"]) if li.status == "ok" else 0)
        + grad_check,
    )
    consistency = round(1 - len(contradictions) / checks_performed, 3)

    coverage = round(verifiable_w / total_w, 3) if total_w else 0.0
    reliability = round((weighted_v_sum / verifiable_w + 1) / 2, 3) if verifiable_w else 0.5
    mean_judge_conf = round(judge_conf_sum / len(out_claims), 3) if out_claims else 0.0
    confidence = round(
        0.5 * coverage + 0.3 * min(n_sources / 3, 1) + 0.2 * mean_judge_conf, 3
    )
    w = cfg("authenticity.weights")
    # Displayed score: the Spec 11 Stage 6 formula, unchanged, so the dashboard and the
    # evaluation keep reporting the documented number.
    authenticity = round(
        w["alpha"] * reliability + w["beta"] * consistency + w["gamma"] * (1 - infl["inflation_index"]),
        3,
    )
    # The BAND is assigned from `band_score`, which leaves inflation_index out (alpha and beta
    # renormalised). Spec 2.7 says AI-written-text indicators are display-only and never enter
    # the score, band or recommendation, and Stage 6 puts inflation_index in Authenticity; the
    # two cannot both hold for the band, so the global safeguard wins. Otherwise a truthful,
    # buzzword-heavy (or AI-polished, or non-native) resume would score lower and could be
    # banded lower on style alone.
    b_score = band_score(reliability, consistency, w)

    bands = cfg("authenticity.bands")
    # Spec Stage 6: a CONTRADICTED claim on a required skill or on a role forces at least
    # NEEDS_VERIFICATION. A contradicted METRIC is not on that list: it lowers reliability
    # (v = -1) but does not by itself force the band. Cross-source Stage 4 findings (LinkedIn
    # date or title conflicts) are source-contradicted role claims and force it. Resume-only
    # findings (overlap, graduation, anachronism) are not: A-22 already declines to mark a
    # claim CONTRADICTED for them because no second source disagrees. They become
    # verification gaps and a note in the summary instead.
    required_contradiction = any(
        c["status"] == "CONTRADICTED"
        and (c["type"] == "ROLE" or (c["type"] == "SKILL" and c["text"].lower() in req_lower))
        for c in out_claims
    ) or bool(li_findings)
    band = assign_band(confidence, b_score, required_contradiction, bands)

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
    gaps += _resume_only_gaps(resume_only_findings, out_claims)

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
            "band_score": b_score,  # what the band was assigned from: no inflation term
        },
        "inflation_sub_signals": infl["sub_signals"],
        "sources_used": source_status,
        "source_details": source_details,
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
        "authenticity_flags": authenticity_flags(
            gh, out_claims, [p.get("name", "") for p in parsed.get("projects", [])]
        ) + portfolio_flags(pf),
        "verification_gaps": gaps,
        "recruiter_summary": _recruiter_summary(
            gh, out_claims, confidence, n_sources
        ) + _resume_only_note(resume_only_findings),
        "meta": {
            "model": "deterministic-v1",
            "config_version": cfg("app.config_version"),
            "latency_ms": 0,
            "llm_calls": llm_calls,
        },
    }


def band_score(reliability: float, consistency: float, weights: dict) -> float:
    """Authenticity without the inflation term: alpha and beta renormalised to sum to 1.
    This is the only score the band is assigned from (Spec 2.7; see `assess`)."""
    a, b = float(weights["alpha"]), float(weights["beta"])
    return round((a * reliability + b * consistency) / (a + b), 3)


def _resume_only_gaps(findings: list[dict], out_claims: list[dict]) -> list[dict]:
    """Verification gaps (Spec 3.3 shape) for resume-internal findings, so Module D can ask."""
    def claim_id(ctype: str, needle: str | None) -> str:
        needle = (needle or "").lower()
        for c in out_claims:
            if c["type"] == ctype and needle and needle in c["text"].lower():
                return c["claim_id"]
        return out_claims[0]["claim_id"] if out_claims else ""

    gaps = []
    for f in findings[:5]:
        if f["type"] == "overlapping_roles":
            titles = [t for t in f.get("titles", []) if t]
            gap = (claim_id("ROLE", titles[0] if titles else None),
                   "Ask the candidate to confirm the dates and employment type (full-time, "
                   "part-time, contract) of the roles that overlap on the resume: "
                   + " and ".join(f"'{t}'" for t in titles) + ".")
        elif f["type"] == "graduation_inconsistency":
            gap = (claim_id("ROLE", f.get("title")),
                   "Ask the candidate to confirm when this role started relative to their "
                   "studies: " + f["detail"])
        else:
            gap = (claim_id("SKILL", f.get("technology")),
                   "Ask the candidate how long they have actually used this technology: "
                   + f["detail"])
        gaps.append({"claim_id": gap[0], "what_to_verify": gap[1], "priority": "med"})
    return gaps


def _resume_only_note(findings: list[dict]) -> str:
    if not findings:
        return ""
    return (
        f" Note: the resume contains {len(findings)} date or experience item(s) that do not fit "
        "together on their own (for example overlapping dates). No other source disagrees, so "
        "this has not changed the trust band; it is listed as a question to ask the candidate."
    )


def assign_band(confidence: float, authenticity: float, forced: bool, bands: dict) -> str:
    """Spec 11 Stage 6. The confidence floor decides whether there is a headline score at all
    (A-6c: checked first, before the contradiction rule). Above it the thresholds apply to the
    AUTHENTICITY score, not to confidence; a contradiction forces at least NEEDS_VERIFICATION."""
    if confidence < float(bands["min_confidence"]):
        return "INSUFFICIENT_EVIDENCE"
    if forced:
        return "NEEDS_VERIFICATION"
    if authenticity >= float(bands["high_trust"]):
        return "HIGH_TRUST"
    if authenticity >= float(bands["moderate"]):
        return "MODERATE"
    return "NEEDS_VERIFICATION"


def _apply_linkedin_conflict(judged: dict, title: str, company: str | None,
                             findings: list[dict]) -> dict:
    """Spec 11 Stage 3: CONTRADICTED is 'a source conflicts with the claim'. A LinkedIn
    date_conflict or title_mismatch for this resume role is exactly that, so the ROLE claim
    becomes CONTRADICTED with the LinkedIn entry as cited evidence. Resume-internal findings
    (overlapping_roles, graduation_inconsistency, anachronisms) are NOT marked on claims: no
    second source disagrees, and they do not say which of the two claims is the wrong one. They
    stay in `contradictions` (and still force the band) instead."""
    for f in findings:
        if f.get("resume_title") != title or (company and f.get("company") != company):
            continue
        if f["type"] not in ("date_conflict", "title_mismatch"):
            continue
        return {
            "status": "CONTRADICTED",
            "evidence": [{"source": "linkedin", "citation": f["citation"], "note": f["detail"]}],
            "rationale": "The candidate's LinkedIn export differs from the resume for this role. "
            "Profiles are often updated at different times, so this is worth a direct question "
            "rather than a conclusion.",
            "judge_confidence": 0.7,
        }
    return judged


# --- METRIC claims against README figures (Spec 11 Stage 3) --------------------------------
_COUNT_NOUNS = {
    "user", "customer", "request", "record", "row", "event", "message", "query", "transaction",
    "engineer", "developer", "feature", "endpoint", "service", "test", "download", "deployment",
    "session", "order", "job",
}
_QTY = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(%|percent\b|pct\b|ms\b|milliseconds?\b|seconds?\b|secs?\b|s\b|x\b|"
    r"k\b|m\b|million\b|thousand\b|[a-z]+)?"
    r"(?:(?:\s+per\s+|\s*/\s*)([a-z]+))?",
    re.I,
)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}
_CONTEXT_STOP = {
    # function words
    "the", "and", "for", "with", "from", "that", "this", "was", "were", "has", "have", "had",
    "into", "onto", "over", "per", "our", "its", "their", "all", "any", "than", "then", "also",
    "while", "which", "using", "use", "used", "via", "about", "across", "within", "through",
    # words that say "changed by an amount" rather than naming the thing measured
    "reduced", "reduce", "reducing", "cut", "improved", "improve", "improving", "increased",
    "increase", "increasing", "decreased", "decrease", "boosted", "boost", "achieved", "achieve",
    "saved", "grew", "grow", "growth", "lowered", "faster", "slower", "less", "more", "up", "down",
    "percent", "pct", "seconds", "second", "milliseconds", "ms",
    # resume boilerplate and generic verbs
    "cross", "functional", "team", "part", "set", "built", "build", "implemented", "wrote",
    "tuned", "led", "deployed", "designed", "developed", "created", "worked", "helped",
}


# Words that appear in almost any performance sentence. Sharing only one of these does not show
# that two sentences measure the same thing ('deploy time' vs 'run time' are different metrics).
_WEAK_CONTEXT = {"time", "system", "data", "application", "app", "code", "project", "platform",
                 "performance", "speed", "service", "services"}


def _same_subject(claim_ctx: set[str], sentence_ctx: set[str]) -> bool:
    """Meaningful overlap: at least two shared context words, or one that is not generic."""
    shared = claim_ctx & sentence_ctx
    return len(shared) >= 2 or bool(shared - {_stem(w) for w in _WEAK_CONTEXT})


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _context_words(text: str) -> set[str]:
    def keep_noun(m: re.Match) -> str:  # drop the figure and its unit, keep a counted noun
        unit = (m.group(2) or "").lower()
        return f" {unit} " if unit.rstrip("s") in _COUNT_NOUNS else " "

    return {_stem(w) for w in re.findall(r"[a-z]+", _QTY.sub(keep_noun, text.lower()))
            if len(w) > 2 and w not in _CONTEXT_STOP}


def _quantities(text: str) -> list[tuple[tuple[str, str], float]]:
    """(unit key, value) pairs. The unit key carries the quantity type and any 'per <unit>'
    denominator, so only like-for-like figures are ever compared."""
    out = []
    for m in _QTY.finditer(text):
        val = float(m.group(1).replace(",", ""))
        unit, per = (m.group(2) or "").lower(), (m.group(3) or "").lower()
        key: tuple[str, str] | None = None
        if unit in ("%", "percent", "pct"):
            key = ("pct", "")
        elif unit in ("ms", "millisecond", "milliseconds"):
            key = ("time", per)
        elif unit in ("s", "sec", "secs", "second", "seconds"):
            key, val = ("time", per), val * 1000
        elif unit == "x":
            key = ("mult", "")
        elif unit in _SCALE:  # 38k users: the scale word must be followed by a counted noun
            nxt = re.match(r"\s*([a-z]+)", text[m.end():])
            word = nxt.group(1).rstrip("s") if nxt else ""
            if word in _COUNT_NOUNS:
                key, val = (f"count:{word}", per), val * _SCALE[unit]
        elif unit.rstrip("s") in _COUNT_NOUNS:
            key = (f"count:{unit.rstrip('s')}", per)
        if key is not None:
            out.append((key, val))
    return out


def _close(a: float, b: float, key: tuple[str, str]) -> bool:
    tol = max(0.5, 0.02 * max(abs(a), abs(b))) if key[0] == "pct" else 0.02 * max(abs(a), abs(b))
    return abs(a - b) <= tol


def judge_metric_claim(text: str, gh: GitHubEvidence) -> dict:
    """Compare a METRIC claim with figures stated in the candidate's own repository READMEs.

    Conservative by design (Spec 2.4 and the METRIC rule in Stage 3):
    - only like-for-like quantities are compared (percent vs percent, time vs time, a count of
      the same noun with the same 'per' denominator);
    - the README sentence must share at least one context word with the claim to CONFIRM it, and
      meaningful context to CONTRADICT it: two context words, or one that is not generic (a
      lone 'time' does not make 'deploy time' the same metric as 'run time');
    - only owned, non-fork repositories with enough authorship are used;
    - a claim with two figures of the same type (e.g. 'from 8 seconds to 5 seconds') can be
      confirmed by an equal figure but is never contradicted, since which one the README
      refers to is ambiguous.
    Everything else stays UNVERIFIABLE, never UNSUPPORTED."""
    base = {
        "status": "UNVERIFIABLE", "evidence": [],
        "rationale": "No code artifact could confirm this metric.", "judge_confidence": 0.0,
    }
    claim_q = _quantities(text)
    if not gh.has_data or not claim_q:
        return base
    claim_ctx = _context_words(text)
    min_auth = float(cfg("authenticity.github.authorship_ratio_min", 0.30))
    from collections import Counter
    per_key = Counter(k for k, _ in claim_q)
    verified = conflict = None
    for repo in gh.repos:
        if repo.fork or not repo.readme_excerpt:
            continue
        if not (repo.authorship_ratio >= min_auth or (repo.owned and repo.commits_total == 0)):
            continue
        cite = f"github.com/{gh.username}/{repo.name}"
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", repo.readme_excerpt):
            sentence_ctx = _context_words(sentence)
            if not claim_ctx & sentence_ctx:
                continue
            strong = _same_subject(claim_ctx, sentence_ctx)
            for rkey, rval in _quantities(sentence):
                for ckey, cval in claim_q:
                    if ckey != rkey:
                        continue
                    if _close(cval, rval, ckey):
                        # An equal figure of the same type is strong evidence by itself, so a
                        # single shared context word is enough to confirm.
                        verified = verified or (cite, cval, rval, ckey)
                    elif per_key[ckey] == 1 and strong:
                        # Asserting a conflict needs more certainty that both sentences are
                        # about the same thing than confirming does.
                        conflict = conflict or (cite, cval, rval, ckey)
    def fmt(v: float, key: tuple[str, str]) -> str:
        n = f"{v:g}"
        return f"{n} percent" if key[0] == "pct" else n
    if verified:
        cite, cval, rval, key = verified
        return {
            "status": "VERIFIED",
            "evidence": [{"source": "github", "citation": cite,
                          "note": f"README states {fmt(rval, key)} for the same quantity"}],
            "rationale": "The repository README states the same figure for the same quantity.",
            "judge_confidence": 0.7,
        }
    if conflict:
        cite, cval, rval, key = conflict
        return {
            "status": "CONTRADICTED",
            "evidence": [{"source": "github", "citation": cite,
                          "note": f"README states {fmt(rval, key)}; resume states {fmt(cval, key)}"}],
            "rationale": f"The repository README gives {fmt(rval, key)} for what appears to be "
            f"the same quantity, while the resume says {fmt(cval, key)}. It may be a different "
            "measurement or a later change, so it is worth asking the candidate.",
            "judge_confidence": 0.6,
        }
    return base


def _build_evidence_index(gh: GitHubEvidence, li: LinkedInEvidence, pf: PortfolioEvidence) -> dict:
    """Stable citation id -> short structured summary of each collected artifact."""
    idx: dict[str, dict] = {}
    if gh.has_data:
        for repo in gh.repos:
            idx[f"github.com/{gh.username}/{repo.name}"] = {
                "source": "github", "authorship_ratio": repo.authorship_ratio,
                "summary": (f"repo {repo.name}; languages={sorted(repo.languages)}; "
                            f"topics={repo.topics}; manifests={repo.manifests_found}; "
                            f"flags={repo.flags}; authorship_ratio={repo.authorship_ratio}"),
                "untrusted_text": repo.readme_excerpt[:1500],
                "_repo": repo,  # code-side grounding only; never sent to the model
            }
    if li.status == "ok":
        for role in li.roles:
            idx[f"linkedin_export:role:{role.title}"] = {
                "source": "linkedin", "authorship_ratio": None,
                "summary": "LinkedIn export role entry",
                "untrusted_text": f"{role.title} at {role.company} ({role.start} to {role.end})",
            }
    if pf.status == "ok" and pf.url:
        idx[pf.url] = {
            "source": "portfolio", "authorship_ratio": None,
            "summary": f"portfolio page; tech_mentions={pf.tech_mentions}; projects={pf.projects[:10]}",
            "untrusted_text": pf.text[:1500],
        }
    return idx


_PARTIAL_NOTE = (
    "GitHub could only be partly checked ({reason}). This is a limit of our data collection "
    "and is not evidence against the candidate; claims that depended on the unchecked "
    "repositories are marked unverifiable rather than unsupported."
)


def _recruiter_summary(gh: GitHubEvidence, claims: list[dict], confidence: float, n_sources: int) -> str:
    text = _recruiter_summary_core(gh, claims, confidence, n_sources)
    if gh.status == "partial":
        return _PARTIAL_NOTE.format(reason=gh.partial_reason) + " " + text
    return text


def _recruiter_summary_core(gh: GitHubEvidence, claims: list[dict], confidence: float, n_sources: int) -> str:
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
    bits = (
        [f"GitHub evidence was reviewed across {len(gh.repos)} repositories."]
        if gh.has_data else []
    )
    if verified:
        bits.append(f"{verified} claim(s) have direct code or manifest evidence.")
    if unsupported:
        bits.append(
            f"{unsupported} claim(s) have no supporting evidence in the sources checked, "
            "which is worth a direct question in interview rather than an assumption either way."
        )
    bits.append(f"Overall assessment confidence is {confidence}.")
    return " ".join(bits)
