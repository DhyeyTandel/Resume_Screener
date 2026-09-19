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


_SECTION = re.compile(
    r"^\s*(skills?|technical skills?|experience|work experience|employment|education|"
    r"projects?|certifications?|summary|profile|achievements?)\s*:?\s*$",
    re.I,
)
_DATE_RANGE = re.compile(
    r"(\d{4})\s*(?:-|–|to)\s*(\d{4}|present|current)", re.I
)


def structure_resume(text: str) -> dict:
    """Section-aware resume structuring on the visible text."""
    sections: dict[str, list[str]] = {}
    current = "summary"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SECTION.match(line)
        if m:
            current = m.group(1).lower()
            current = (
                "skills" if "skill" in current
                else "experience" if current in ("experience", "work experience", "employment")
                else "projects" if current.startswith("project")
                else "education" if current == "education"
                else "certifications" if current.startswith("cert")
                else "achievements" if current.startswith("achiev")
                else "summary"
            )
            continue
        sections.setdefault(current, []).append(line.lstrip("-*• ").strip())

    skills: list[str] = []
    for line in sections.get("skills", []):
        for part in re.split(r"[,;|/]| and ", line):
            part = part.strip(" .:")
            if part and 1 <= len(part.split()) <= 4 and canonical(part):
                skills.append(part)
    # Skills named inside experience/projects also count as skills.
    body = " ".join(sections.get("experience", []) + sections.get("projects", []))
    for tok in _TECHish.findall(body):
        if canonical(tok) and tok.lower() not in {s.lower() for s in skills}:
            skills.append(tok)

    experience = []
    for line in sections.get("experience", []):
        m = _DATE_RANGE.search(line)
        if m:
            experience.append(
                {
                    "title": line.split(",")[0].split(" at ")[0].strip(),
                    "company": (line.split(" at ")[-1].split(",")[0].strip() if " at " in line else ""),
                    "start": m.group(1),
                    "end": m.group(2),
                    "relevant_points": [],
                }
            )
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
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return float(sum(e - s for s, e in merged))


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
