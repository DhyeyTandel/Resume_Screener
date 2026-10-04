"""Module Core: JD requirements, resume structuring, requirement matching (Spec 9)."""
from __future__ import annotations

import re

from ...config import cfg
from ...schemas.vocab import MISSING, NOT_ENOUGH, classification_to_status
from ..skill_intelligence.transfer import analyze_skill, canonical

_MUST = re.compile(r"\b(must|required|requirement|essential|minimum|need|strong)\b", re.I)
_PREF = re.compile(r"\b(preferred|nice to have|bonus|plus|desirable|optional)\b", re.I)
_YEARS = re.compile(r"(\d+)\s*\+?\s*(?:years|yrs)", re.I)
_TECHish = re.compile(r"[A-Za-z][\w.+#/-]{1,24}")

CATEGORIES = {
    "language": "language",
    "framework": "framework/tool",
    "database": "framework/tool",
    "messaging": "framework/tool",
    "infra": "framework/tool",
    "cloud": "framework/tool",
    "testing": "framework/tool",
    "concept": "technical skill",
}


def extract_requirements(jd_text: str) -> list[dict]:
    """Heuristic JD requirement extraction (deterministic; the mock-mode path)."""
    reqs: list[dict] = []
    seen: set[str] = set()
    section_priority = "Must Have"
    for raw in jd_text.splitlines():
        line = raw.strip(" -*•\t")
        if not line:
            continue
        if _PREF.search(line) and len(line.split()) <= 6:
            section_priority = "Preferred"
            continue
        if _MUST.search(line) and len(line.split()) <= 6:
            section_priority = "Must Have"
            continue
        priority = (
            "Preferred" if _PREF.search(line) else "Must Have" if _MUST.search(line)
            else section_priority
        )
        years = _YEARS.search(line)
        for token in _TECHish.findall(line):
            skill = canonical(token)
            if skill and skill not in seen:
                seen.add(skill)
                node = _node(skill)
                reqs.append(
                    {
                        "id": f"R{len(reqs) + 1}",
                        "requirement": _display(skill),
                        "category": CATEGORIES.get(node.get("category", ""), "technical skill"),
                        "priority": priority,
                        "min_years": int(years.group(1)) if years else None,
                    }
                )
        if years and not any(r["category"] == "min experience" for r in reqs):
            reqs.append(
                {
                    "id": f"R{len(reqs) + 1}",
                    "requirement": f"{years.group(1)}+ years of professional experience",
                    "category": "min experience",
                    "priority": priority,
                    "min_years": int(years.group(1)),
                }
            )
        if re.search(r"\b(bachelor|b\.?s\.?|b\.?tech|degree)\b", line, re.I) and not any(
            r["category"] == "education" for r in reqs
        ):
            reqs.append(
                {
                    "id": f"R{len(reqs) + 1}",
                    "requirement": "Bachelor's degree in a computing field",
                    "category": "education",
                    "priority": priority,
                    "min_years": None,
                }
            )
    return reqs


def _node(skill: str) -> dict:
    from ..skill_intelligence.transfer import graph

    return graph()["skills"].get(skill, {})


def _display(skill: str) -> str:
    special = {
        "fastapi": "FastAPI", "postgresql": "PostgreSQL", "mysql": "MySQL", "aws": "AWS",
        "gcp": "GCP", "sql": "SQL", "ci/cd": "CI/CD", "rest apis": "REST APIs",
        "javascript": "JavaScript", "typescript": "TypeScript", "kafka": "Kafka",
        "mongodb": "MongoDB", "sqlite": "SQLite", "rabbitmq": "RabbitMQ",
        "kubernetes": "Kubernetes", "python": "Python", "django": "Django", "flask": "Flask",
    }
    return special.get(skill, skill.title())


_SECTION_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("skills", re.compile(
        r"(?:(?:technical|core|key|professional|relevant)\s+)?"
        r"(?:skills?|competenc(?:y|ies)|proficienc(?:y|ies)|technologies|tech stack)"
        r"(?:\s*(?:&|and)\s*[a-z ]{2,25})?")),
    ("experience", re.compile(
        r"(?:(?:work|professional|relevant|employment|career)\s+)?(?:experience|history)"
        r"|employment|work")),
    ("projects", re.compile(r"(?:(?:personal|academic|selected|key|side|notable)\s+)?projects?")),
    ("education", re.compile(r"education(?:\s*(?:&|and)\s*training)?|academics?|academic background")),
    ("certifications", re.compile(
        r"certifications?(?:\s*(?:&|and)\s*licen[sc]es)?|licen[sc]es(?:\s*(?:&|and)\s*certifications?)?")),
    ("achievements", re.compile(
        r"(?:(?:key|notable|major|selected)\s+)?(?:achievements?|accomplishments?|awards?|honou?rs?)"
        r"(?:\s*(?:&|and)\s*(?:achievements?|accomplishments?|awards?|honou?rs?|recognition))?")),
    ("summary", re.compile(
        r"(?:(?:professional|career|executive)\s+)?(?:summary|profile|objective)|about(?: me)?")),
]
_MONTH_TAIL = re.compile(
    r"[\s,|(:\u2013\u2014-]*(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*)?$",
    re.I,
)
_DATE_RANGE = re.compile(
    r"(\d{4})\s*(?:-|\u2013|\u2014|to)\s*(?:[a-z]{3,9}\.?\s+)?(\d{4}|present|current)", re.I
)
_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z &/+.-]{0,30}?)\s*:\s*(\S.*)$")
_CATEGORY_WORDS = {
    "language", "languages", "framework", "frameworks", "tool", "tools", "database", "databases",
    "cloud", "devops", "other", "others", "library", "libraries", "platform", "platforms",
    "technology", "technologies", "testing", "backend", "frontend", "front-end", "back-end",
    "web", "data", "ml", "ai", "os", "version control", "methodologies", "concepts", "misc",
    "infrastructure", "messaging", "monitoring", "scripting",
}
# Slash compounds that are one name, never two skills.
_SLASH_COMPOUND = re.compile(
    r"^(?:ci/cd|tcp/ip|udp/ip|pl/sql|pl/pgsql|ui/ux|ux/ui|i/o|a/b|ai/ml|ml/ai)$", re.I
)


def _heading(line: str) -> str | None:
    """Return the canonical section key if `line` is a section heading, else None."""
    if len(line) > 44:
        return None
    norm = re.sub(r"[\s_]+", " ", line.strip().strip("#*_:= -").strip()).lower()
    if not norm:
        return None
    for key, rx in _SECTION_RULES:
        if rx.fullmatch(norm):
            return key
    return None


def _split_slash(part: str) -> list[str]:
    """Split "Docker/Kubernetes" but keep known compounds ("CI/CD", "TCP/IP") intact.

    A part that is itself a skill-graph alias (e.g. "ci/cd") is never split.
    """
    from ..skill_intelligence.transfer import graph

    if "/" not in part or _SLASH_COMPOUND.match(part.strip()):
        return [part]
    if part.strip().lower() in graph()["_alias"]:
        return [part]
    return [p for p in (x.strip() for x in part.split("/")) if p]


def _strip_label(line: str) -> tuple[str | None, str]:
    """Split a grouped skills line "Languages: Python, Go" into (label, rest)."""
    m = _LABEL.match(line)
    if not m:
        return None, line
    label, rest = m.group(1).strip(), m.group(2)
    if len(label.split()) > 4:
        return None, line
    return label, rest


def _skills_from_line(line: str) -> list[str]:
    out: list[str] = []
    label, rest = _strip_label(line)
    if label and label.lower() not in _CATEGORY_WORDS and canonical(label):
        out.append(label)  # "AWS: S3, EC2": the label is itself a skill
    for chunk in re.split(r"[,;|\u2022]| and ", rest):
        for part in _split_slash(chunk):
            part = part.strip(" .:()")
            if part and 1 <= len(part.split()) <= 4 and canonical(part):
                out.append(part)  # any length: the skills section is the trusted place
    return out


def _body_skill_tokens(body: str) -> list[str]:
    """Known skills named in prose. Short names (Go, JS) need capitalisation mid-sentence."""
    out: list[str] = []
    for m in _TECHish.finditer(body):
        raw = m.group(0)
        for tok in _split_slash(raw):
            tok = tok.strip(".-/")
            if not tok or not canonical(tok):
                continue
            if len(tok) <= 3:
                before = body[: m.start()].rstrip()
                sentence_start = not before or before[-1] in ".!?:"
                if tok.islower() or sentence_start:
                    continue  # "go live" or "Go to market" is prose, not a language
            out.append(tok)
    return out


def _parse_role(line: str, dates: re.Match[str]) -> dict:
    """Title and company from "Title at Company, dates", "Title, Company, dates",
    "Title | Company | dates" or "Title - Company (dates)"."""
    head = _MONTH_TAIL.sub("", line[: dates.start()])
    head = head.rstrip(" ,|(:\u2013\u2014-")
    title, company = head, ""
    at = re.split(r"\s+(?:at|@)\s+", head, maxsplit=1, flags=re.I)
    if len(at) == 2:
        title = at[0]
        company = re.split(r"[,|]", at[1])[0]
    else:
        parts = [p.strip() for p in re.split(r"\s*[,|]\s*|\s+[\u2013\u2014-]\s+", head) if p.strip()]
        if parts:
            title = parts[0]
            company = parts[1] if len(parts) > 1 else ""
    return {
        "title": title.strip(),
        "company": company.strip(),
        "start": dates.group(1),
        "end": dates.group(2),
        "relevant_points": [],
    }


def structure_resume(text: str) -> dict:
    """Section-aware resume structuring on the visible text."""
    sections: dict[str, list[str]] = {}
    current = "summary"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key = _heading(line)
        if key:
            current = key
            continue
        sections.setdefault(current, []).append(line.lstrip("-*\u2022 ").strip())

    skills: list[str] = []
    for line in sections.get("skills", []):
        skills.extend(_skills_from_line(line))
    # Skills named inside experience/projects also count as skills.
    body = " ".join(sections.get("experience", []) + sections.get("projects", []))
    for tok in _body_skill_tokens(body):
        if tok.lower() not in {s.lower() for s in skills}:
            skills.append(tok)

    experience: list[dict] = []
    for line in sections.get("experience", []):
        m = _DATE_RANGE.search(line)
        if m:
            experience.append(_parse_role(line, m))
        elif experience:
            experience[-1]["relevant_points"].append(line)

    projects = []
    for line in sections.get("projects", []):
        name, _, desc = line.partition(":")
        projects.append({"name": name.strip()[:80], "description": (desc or line).strip()})

    return {
        "summary": " ".join(sections.get("summary", []))[:600],
        "skills": sorted(set(skills), key=str.lower),
        "experience": experience,
        "projects": projects,
        "education": sections.get("education", []),
        "certifications": sections.get("certifications", []),
        "achievements": sections.get("achievements", []),
        "total_years": total_years(experience),
    }


def total_years(experience: list[dict]) -> float:
    """Years of experience computed deterministically from dates (union of ranges)."""
    spans = []
    for e in experience:
        try:
            start = int(e["start"])
            end = 2026 if str(e["end"]).lower() in ("present", "current") else int(e["end"])
        except (ValueError, KeyError, TypeError):
            continue
        if end >= start:
            spans.append((start, end))
    if not spans:
        return 0.0
    spans.sort()
    merged = [list(spans[0])]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return float(sum(hi - lo for lo, hi in merged))


def has_enough_content(resume: dict) -> bool:
    m = cfg("core.min_evidence_for_missing")
    return len(resume["skills"]) >= int(m["skills"]) and (
        len(resume["projects"]) + len(resume["experience"]) >= int(m["projects_or_roles"])
    )


def match_requirements(requirements: list[dict], resume: dict) -> list[dict]:
    """Status per requirement, with Module C's transferability block attached."""
    enough = has_enough_content(resume)
    out = []
    for req in requirements:
        if req["category"] == "min experience":
            years = resume["total_years"]
            need = req.get("min_years") or 0
            if not resume["experience"]:
                status, note = NOT_ENOUGH, "No dated roles found in the resume."
            elif years >= need:
                status, note = "Matched", f"{years:.0f} years computed from dated roles."
            else:
                status, note = MISSING, f"{years:.0f} years found against {need} required."
            out.append({**req, "status": status, "resume_evidence": "", "notes": note,
                        "transferability": None})
            continue
        if req["category"] == "education":
            blob = " ".join(resume["education"])
            if not blob:
                status, note = NOT_ENOUGH, "No education section was found."
            elif re.search(r"bachelor|b\.?s\.?\b|b\.?tech|master|m\.?s\.?\b", blob, re.I):
                status, note = "Matched", "A degree at or above the required level was found."
            else:
                status, note = NOT_ENOUGH, "The education section did not state a degree level."
            out.append({**req, "status": status, "resume_evidence": blob[:160], "notes": note,
                        "transferability": None})
            continue

        c = analyze_skill(req["requirement"], resume)
        status = classification_to_status(
            c["classification"], resume_has_enough_content=enough
        )
        out.append(
            {
                **req,
                "status": status,
                "resume_evidence": _evidence_for(req["requirement"], resume),
                "notes": c["recruiter_suggestion"],
                "transferability": c,
                "transferability_score": (
                    c["transferability_score"] if status == "Partially Matched" else None
                ),
            }
        )
    return out


def _evidence_for(skill: str, resume: dict) -> str:
    needle = skill.lower()
    for p in resume["projects"]:
        blob = f"{p['name']}: {p['description']}"
        if needle in blob.lower():
            return blob[:200]
    for e in resume["experience"]:
        for pt in e["relevant_points"]:
            if needle in pt.lower():
                return pt[:200]
    for s in resume["skills"]:
        if needle == s.lower():
            return f"Listed in skills: {s}"
    return ""
