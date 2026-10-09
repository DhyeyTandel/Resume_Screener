"""LinkedIn evidence collector (Module B Stage 2).

Spec: no scraping of linkedin.com. Only a candidate-provided "Save to PDF"
export or a structured JSON export is accepted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class LinkedInRole:
    title: str
    company: str
    start: str
    end: str  # "Present" or a year


@dataclass
class LinkedInEvidence:
    status: str  # ok | missing | no_consent | error
    roles: list[LinkedInRole] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    error: str | None = None


# "2020 - Present", "January 2021 - Present (5 years)", "June 2019 - December 2020": LinkedIn's
# PDF puts a month name before each year.
_MONTH_WORD = r"(?:(?!present|current)[A-Za-z]{3,9}\.?\s+)?"
_DATE_RANGE = re.compile(
    rf"{_MONTH_WORD}(\d{{4}})\s*(?:-|–|—|to)\s*{_MONTH_WORD}(\d{{4}}|present|current)", re.I
)
# Section headings of a LinkedIn PDF. Dated lines under Education etc. are not jobs.
_SECTION = re.compile(
    r"^(experience|education|skills|top skills|certifications|licenses & certifications|"
    r"languages|honors-awards|honors & awards|publications|projects|volunteer experience|"
    r"summary|contact|courses|patents|recommendations)$", re.I
)


def parse_structured_json(data: dict) -> LinkedInEvidence:
    """The reliable path: a candidate-exported structured JSON document."""
    try:
        roles = [
            LinkedInRole(
                title=r.get("title", ""), company=r.get("company", ""),
                start=str(r.get("start", "")), end=str(r.get("end", "")),
            )
            for r in data.get("roles", [])
        ]
        return LinkedInEvidence(
            status="ok", roles=roles,
            education=data.get("education", []),
            skills=data.get("skills", []),
            certifications=data.get("certifications", []),
        )
    except Exception as exc:
        return LinkedInEvidence(status="error", error=f"malformed LinkedIn JSON: {exc}")


def parse_pdf_export_text(text: str) -> LinkedInEvidence:
    """Best-effort text parse of a 'Save to PDF' export. Looser than the
    structured path since LinkedIn's PDF layout is not a stable format."""
    if not text or not text.strip():
        return LinkedInEvidence(status="error", error="empty LinkedIn export")
    roles: list[LinkedInRole] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    has_experience = any(ln.lower() == "experience" for ln in lines)
    section = ""
    for i, line in enumerate(lines):
        if _SECTION.match(line):
            section = line.lower()
            continue
        if has_experience and section != "experience":
            continue  # education, certifications and the like carry dates but are not jobs
        m = _DATE_RANGE.search(line)
        if not m:
            continue
        title = lines[i - 1] if i > 0 else ""
        company = lines[i - 2] if i > 1 else ""
        roles.append(
            LinkedInRole(
                title=title, company=company,
                start=m.group(1), end=m.group(2).title() if not m.group(2).isdigit() else m.group(2),
            )
        )
    if not roles:
        return LinkedInEvidence(
            status="error", error="no dated roles found in the LinkedIn export text"
        )
    return LinkedInEvidence(status="ok", roles=roles)


def collect_linkedin(linkedin: dict | None) -> LinkedInEvidence:
    """`linkedin`: {"type": "pdf_export"|"structured_json"|None, "content": str|dict}."""
    if not linkedin or not linkedin.get("content"):
        return LinkedInEvidence(status="missing")
    kind = linkedin.get("type")
    content = linkedin["content"]
    if kind == "unreadable_export":
        # The upload step could not read the file (e.g. a malformed PDF): a clean error status.
        return LinkedInEvidence(status="error", error=f"unreadable LinkedIn export: {content}")
    try:
        if kind == "structured_json":
            return parse_structured_json(content if isinstance(content, dict) else {})
        if kind == "pdf_export":
            return parse_pdf_export_text(str(content))
        return LinkedInEvidence(status="error", error=f"unknown LinkedIn export type: {kind}")
    except Exception as exc:
        return LinkedInEvidence(status="error", error=str(exc))
