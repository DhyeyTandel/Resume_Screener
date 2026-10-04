"""redact_for_scoring: strip protected attributes before any scoring LLM call."""
from __future__ import annotations

import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
# A bare "2019 - 2026" year range satisfies the loose PHONE pattern above; it
# must never be redacted (dates are job-relevant, kept for scoring). Skip a
# PHONE match that is exactly two 4-digit years joined by one dash/space run.
_YEAR_RANGE = re.compile(r"^(19|20)\d{2}\s*[-–]\s*(19|20)\d{2}$")
URL = re.compile(r"https?://\S+|(?:www\.)\S+")
_DROP = re.compile(
    r"^\s*(date of birth|dob|age|gender|sex|marital status|nationality|"
    r"religion|caste|race|ethnicity|photo|photograph)\b.*$",
    re.I | re.M,
)
_INSTITUTION = re.compile(
    r"\b(?:[A-Z][\w&.'-]+\s+){0,4}"
    r"(University|Institute of Technology|Institute|College|Polytechnic|School of \w+)"
    r"(?:\s+of\s+[A-Z][\w.'-]+)?",
)
_DEGREE = re.compile(
    r"\b(B\.?\s?Tech|B\.?\s?E\.?|B\.?\s?Sc|BS|Bachelor(?:'s)?|M\.?\s?Tech|M\.?\s?Sc|MS|"
    r"Master(?:'s)?|MBA|Ph\.?\s?D|Doctorate|Diploma)\b",
    re.I,
)


_NAME_TOKEN = re.compile(r"^[A-Z][a-zA-Z'\-]+\.?$")
_ROLE_WORDS = {
    "engineer", "developer", "manager", "analyst", "scientist", "designer", "architect",
    "consultant", "intern", "lead", "senior", "junior", "resume", "curriculum", "vitae",
    "summary", "profile", "experience", "skills", "education", "backend", "frontend",
}


def guess_candidate_name(text: str) -> str | None:
    """The conventional resume header: a first line of 2-4 capitalised words with no
    digits or role words, followed within three lines by a contact line. The contact
    condition is what stops a title header like 'Senior Backend Engineer' being taken
    for a name. Returns None when unsure; redaction then masks nothing extra."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    first = lines[0]
    tokens = first.split()
    if not 2 <= len(tokens) <= 4 or not all(_NAME_TOKEN.match(t) for t in tokens):
        return None
    if any(t.lower().strip(".") in _ROLE_WORDS for t in tokens):
        return None
    if not any(EMAIL.search(ln) or PHONE.search(ln) or URL.search(ln) for ln in lines[1:4]):
        return None
    return first


def redact_for_scoring(text: str, *, candidate_name: str | None = None) -> str:
    """Mask identity and protected attributes; keep degree level + field.

    When no name is supplied (file uploads only know the filename), the resume's own
    header line is detected and masked, so the name cannot survive into the profile."""
    candidate_name = candidate_name or guess_candidate_name(text)
    out = _DROP.sub("", text)
    if candidate_name:
        for part in [candidate_name] + candidate_name.split():
            if len(part) > 2:
                out = re.sub(rf"\b{re.escape(part)}\b", "[CANDIDATE]", out, flags=re.I)
    out = EMAIL.sub("[CONTACT]", out)
    out = PHONE.sub(lambda m: m.group(0) if _YEAR_RANGE.match(m.group(0).strip()) else "[CONTACT]", out)
    out = URL.sub("[CONTACT]", out)
    out = _INSTITUTION.sub("[INSTITUTION]", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def has_protected_attributes(text: str) -> bool:
    return bool(_DROP.search(text) or EMAIL.search(text) or _INSTITUTION.search(text))
