"""Module Core: JD requirements, resume structuring, requirement matching (Spec 9)."""
from __future__ import annotations

import re
from datetime import date

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
    return _scan_jd(jd_text)[0]


def unrecognised_jd_lines(jd_text: str) -> list[str]:
    """Requirement-looking JD lines that produced no requirement (Spec 9.1).

    A skill outside the graph (Terraform, Redis, GraphQL, Rust) is not silently dropped from
    the report: its line is surfaced here so the recruiter knows coverage was partial."""
    return _scan_jd(jd_text)[1]


_BULLET = re.compile(r"^\s*[-*\u2022]\s+")
_MAX_UNRECOGNISED = 20


def _scan_jd(jd_text: str) -> tuple[list[dict], list[str]]:
    reqs: list[dict] = []
    unrecognised: list[str] = []
    seen: set[str] = set()
    section_priority = "Must Have"
    in_req_section = False  # inside a Must have / Preferred block
    saw_req_header = False
    for raw in jd_text.splitlines():
        line = raw.strip(" -*•\t")
        if not line:
            continue
        before = len(reqs)
        if _PREF.search(line) and len(line.split()) <= 6:
            section_priority = "Preferred"
            in_req_section = saw_req_header = True
            continue
        if _MUST.search(line) and len(line.split()) <= 6:
            section_priority = "Must Have"
            in_req_section = saw_req_header = True
            continue
        is_bullet = bool(_BULLET.match(raw))
        priority = (
            "Preferred" if _PREF.search(line) else "Must Have" if _MUST.search(line)
            else section_priority
        )
        years = _YEARS.search(line)
        recognised = False
        for token in _TECHish.findall(line):
            skill = canonical(token)
            if skill:
                recognised = True
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
        if years:
            recognised = True
        if re.search(r"\b(bachelor|b\.?s\.?|b\.?tech|degree)\b", line, re.I):
            recognised = True
            if not any(r["category"] == "education" for r in reqs):
                reqs.append(
                    {
                        "id": f"R{len(reqs) + 1}",
                        "requirement": "Bachelor's degree in a computing field",
                        "category": "education",
                        "priority": priority,
                        "min_years": None,
                    }
                )
        if not recognised and not is_bullet and len(line.split()) <= 3:
            in_req_section = False  # a plain heading such as "Benefits" ends the block
            continue
        candidate = in_req_section or (not saw_req_header and is_bullet)
        if candidate and not recognised and len(reqs) == before:
            if len(unrecognised) < _MAX_UNRECOGNISED:
                unrecognised.append(line[:200])
    return reqs, unrecognised


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
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_DATE_RANGE = re.compile(
    rf"(?:\b({_MON})\.?,?\s+|\b(\d{{1,2}})/)?(\d{{4}})\s*(?:-|\u2013|\u2014|to)\s*"
    rf"(?:({_MON})\.?,?\s+|(\d{{1,2}})/)?(\d{{4}}|present|current|now|today)\b",
    re.I,
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


def _is_skills_label(label: str) -> bool:
    norm = re.sub(r"[\s_]+", " ", label.strip()).lower()
    return bool(_SECTION_RULES[0][1].fullmatch(norm)) or norm in _CATEGORY_WORDS


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


def _month_number(name: str | None, num: str | None) -> int | None:
    """1-12 from "Jan"/"January" or a numeric "03/", else None (year-only date)."""
    if name:
        return _MONTHS.index(name[:3].lower()) + 1
    if num and 1 <= int(num) <= 12:
        return int(num)
    return None


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
    end = dates.group(6)
    if end.lower() in ("now", "today"):
        end = "present"
    return {
        "title": title.strip(),
        "company": company.strip(),
        "start": dates.group(3),
        "end": end,
        "start_month": _month_number(dates.group(1), dates.group(2)),
        "end_month": _month_number(dates.group(4), dates.group(5)),
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
    # Labelled skill lines ("Skills: Python, FastAPI", "Tech stack: ...", "Languages: ...")
    # count wherever they sit; absence of a Skills heading is not absence of skills.
    for name, lines in sections.items():
        if name in ("skills", "education", "certifications"):
            continue
        for line in lines:
            label, _rest = _strip_label(line)
            if label and (_is_skills_label(label)):
                skills.extend(_skills_from_line(line))
    # Skills named in prose also count, under any heading or none (summary, experience,
    # projects, achievements), never from the education or certification blocks.
    prose = [
        ln for name, lines in sections.items()
        if name not in ("skills", "education", "certifications") for ln in lines
    ]
    body = " ".join(prose)
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


def total_years(experience: list[dict], today: date | None = None) -> float:
    """Years of experience computed deterministically from dates (union of ranges).

    Month-level when the resume gives months ("Jan 2022 - Dec 2024" is 3.0 years, both
    months inclusive). A year-only bound counts from January, so "2019 - 2021" stays 2.0
    years. "Present" ends at the current month, read from the clock."""
    today = today or date.today()
    spans = []
    for e in experience:
        try:
            sm = int(e.get("start_month") or 1)
            start = int(e["start"]) * 12 + sm - 1
            if str(e["end"]).lower() in ("present", "current"):
                end = today.year * 12 + today.month  # exclusive: the current month counts
            else:
                # exclusive end: a named month counts in full; a bare year ends where it starts
                end = int(e["end"]) * 12 + int(e.get("end_month") or 0)
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
    return round(sum(hi - lo for lo, hi in merged) / 12, 2)


def has_enough_content(resume: dict) -> bool:
    m = cfg("core.min_evidence_for_missing")
    return len(resume["skills"]) >= int(m["skills"]) and (
        len(resume["projects"]) + len(resume["experience"]) >= int(m["projects_or_roles"])
    )


# Degree table (Spec 9.3), most senior first. Word-boundary patterns only: "Diploma in
# Information Systems" and "Clubs" must not read as a degree. Words match any case; the short
# initialisms ("BE", "BS", "MS") match only as written in capitals, so "be" never counts.
_DEGREE_LEVELS: list[tuple[int, str, re.Pattern[str]]] = [
    (3, "doctorate", re.compile(
        r"(?<![A-Za-z])(?:(?i:ph\.?\s?d|doctorate|doctor of philosophy)|D\.?Phil)(?![A-Za-z])")),
    (2, "master's", re.compile(
        r"(?<![A-Za-z])(?:(?i:master'?s?|m\.?\s?tech|m\.?\s?sc|m\.?\s?eng|mba|mca)"
        r"|M\.S\.?|MS|M\.A\.|M\.E\.)(?![A-Za-z])")),
    (1, "bachelor's", re.compile(
        r"(?<![A-Za-z])(?:(?i:bachelor'?s?|b\.?\s?tech|b\.?\s?sc|b\.?\s?eng|b\.?\s?c\.?\s?a"
        r"|bba)|B\.S\.?|BS|B\.A\.?|BA|B\.E\.?|BE)(?![A-Za-z])")),
    (0, "diploma", re.compile(r"(?<![A-Za-z])(?i:diploma|associate'?s? degree)(?![A-Za-z])")),
]


def degree_level(text: str) -> tuple[int, str] | None:
    """Highest degree level named in `text` as (rank, label), or None."""
    for rank, label, rx in _DEGREE_LEVELS:
        if rx.search(text):
            return rank, label
    return None


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
                status, note = "Matched", f"{years:.1f} years computed from dated roles."
            else:
                status, note = MISSING, f"{years:.1f} years found against {need} required."
            out.append({**req, "status": status, "resume_evidence": "", "notes": note,
                        "transferability": None})
            continue
        if req["category"] == "education":
            blob = " ".join(resume["education"])
            if not blob:
                status, note = NOT_ENOUGH, "No education section was found."
            elif (lvl := degree_level(blob)) and lvl[0] >= 1:
                status, note = "Matched", f"A {lvl[1]} degree, at or above the required level, was found."
            elif lvl:
                status, note = NOT_ENOUGH, (
                    "Only a diploma was found, below the bachelor's level; check manually."
                )
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
