"""redact_for_scoring: strip protected attributes before any scoring LLM call."""
from __future__ import annotations
import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
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


def redact_for_scoring(text: str, *, candidate_name: str | None = None) -> str:
    """Mask identity and protected attributes; keep degree level + field."""
    out = _DROP.sub("", text)
    if candidate_name:
        for part in [candidate_name] + candidate_name.split():
            if len(part) > 2:
                out = re.sub(rf"\b{re.escape(part)}\b", "[CANDIDATE]", out, flags=re.I)
    out = EMAIL.sub("[CONTACT]", out)
    out = PHONE.sub("[CONTACT]", out)
    out = URL.sub("[CONTACT]", out)
    out = _INSTITUTION.sub("[INSTITUTION]", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def has_protected_attributes(text: str) -> bool:
    return bool(_DROP.search(text) or EMAIL.search(text) or _INSTITUTION.search(text))
