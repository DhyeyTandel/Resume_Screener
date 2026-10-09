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
# Well-known institutions that carry no University/College keyword. Short acronyms are matched
# case-sensitively; a job-relevant product name that starts with one (MIT License, Stanford
# CoreNLP) is excluded by lookahead. Deliberately a small curated list, not a name guesser.
_INSTITUTION_NAMED = re.compile(
    r"\b(?:(?:IIT|IIM|IIIT|NIT|BITS|IISc)[\s-]+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"
    r"|IISc|MIT(?!\s+[Ll]icen[sc]e)|Stanford(?!\s+(?:CoreNLP|NLP|Parser))|Harvard|Yale|Princeton"
    r"|Caltech|Cornell|UC\s+Berkeley|UCLA|UCSD|Carnegie\s+Mellon|Georgia\s+Tech|ETH\s+Z[uü]rich"
    r"|EPFL|Tsinghua|NUS|NTU|Imperial\s+College|Johns\s+Hopkins|Wharton)\b",
)
# Religious or identity-affinity groups: a capitalised run that contains a protected word.
_RELIGION_WORD = (
    r"(?:Hindu|Muslim|Islamic|Christian|Catholic|Jewish|Sikh|Buddhist|Jain|Mormon|Evangelical"
    r"|Orthodox|Baptist|Methodist|Quaker|Hillel|Newman)"
)
_AFFINITY = re.compile(
    rf"\b(?:[A-Z][\w'&-]*\s+){{0,3}}{_RELIGION_WORD}(?:\s+[A-Z][\w'&-]*){{0,3}}\b"
    r"|\b(?:Society of Women Engineers|Women in (?:Tech|Technology|Engineering|STEM|Computing)"
    r"|Women Who Code|Black (?:Engineers|Professionals|Students)(?: \w+)?"
    r"|(?:LGBTQ\+?|Queer|Pride|Veterans?)\s+(?:Alliance|Association|Network|Society|Club|Group))\b",
)
# Inline protected attributes that sit inside an otherwise job-relevant line.
_INLINE_ATTR = re.compile(
    r"\(?\bborn\b[:\s]*(?:in\s+)?(?:\d{1,2}(?:st|nd|rd|th)?\s+)?(?:[A-Za-z]{3,9}\s+)?(?:19|20)\d{2}\)?"
    r"|\(?\bage[d]?\b[:\s]*\d{2}\b\)?"
    r"|\b\d{2}[- ]years?[- ]old(?=\s*(?:[,.;)|]|$))"
    r"|\b(?:married|unmarried|divorced|widowed)\b"
    r"|\b(?-i:[A-Z][a-z]+\s+(?:national|nationality|citizen))\b"
    r"|\b(?:she|her|hers|herself|he|him|his|himself)\b"
    r"|\b(?:male|female)\b"
    r"|\b(?-i:(?:Mr|Mrs|Ms|Miss)\.?(?=\s+[A-Z]))",
    re.I | re.M,
)
_DEGREE = re.compile(
    r"\b(B\.?\s?Tech|B\.?\s?E\.?|B\.?\s?Sc|BS|Bachelor(?:'s)?|M\.?\s?Tech|M\.?\s?Sc|MS|"
    r"Master(?:'s)?|MBA|Ph\.?\s?D|Doctorate|Diploma)\b",
    re.I,
)


_NAME_TOKEN = re.compile(r"^[A-Z][a-zA-Z'\-]+\.?$")
_PLAIN_NAME_TOKEN = re.compile(r"^[A-Za-z][A-Za-z'\-]+\.?$")
# Words that are never a person's name: generic file/document words and role words.
_GENERIC_WORDS = {
    "cv", "final", "draft", "copy", "new", "newest", "latest", "updated", "update", "old",
    "version", "doc", "docs", "document", "file", "pdf", "docx", "scan", "scanned", "candidate",
    "application", "applicant", "pasted", "untitled", "template", "sample", "test", "the",
    "and", "for", "of", "jd", "job", "resume", "cv.", "export", "download", "upload",
}
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


def _is_skill_or_generic(token: str) -> bool:
    """A token that must never be masked as a name: a known skill, role word or generic word."""
    t = token.strip(".'-").lower()
    if not t or t in _ROLE_WORDS or t in _GENERIC_WORDS:
        return True
    if re.fullmatch(r"v\d+|\d+", t):
        return True
    from ..modules.skill_intelligence.transfer import canonical  # lazy: avoids an import cycle

    return canonical(t) is not None


def masking_names(text: str, candidate_name: str | None = None) -> list[str]:
    """Names to mask. Only a real person's name drives masking: the one found in the resume's
    own header, or an explicitly provided name that passes the same plausibility test (2-4
    alphabetic words, none a skill, role word or generic word such as Resume, CV, Final, v2).
    A name derived from an upload's filename ("Docker Kafka Cv") fails and masks nothing."""
    out: list[str] = []
    found = guess_candidate_name(text)
    if found:
        out.append(found)
    if candidate_name:
        toks = candidate_name.split()
        if (
            1 <= len(toks) <= 4
            and all(_PLAIN_NAME_TOKEN.match(t) for t in toks)
            and not any(_is_skill_or_generic(t) for t in toks)
            and candidate_name not in out
        ):
            out.append(candidate_name)
    return out


def redact_for_scoring(text: str, *, candidate_name: str | None = None) -> str:
    """Mask identity and protected attributes; keep degree level + field.

    The name masked is the resume's own header name or a plausible explicit name (see
    `masking_names`); tokens that are skills, role words or generic words are never masked."""
    out = _DROP.sub("", text)
    out = EMAIL.sub("[CONTACT]", out)  # before names, so a name inside an address cannot leak
    for name in masking_names(text, candidate_name):
        for part in [name] + name.split():
            if len(part) > 2 and not (part != name and _is_skill_or_generic(part)):
                out = re.sub(rf"\b{re.escape(part)}\b", "[CANDIDATE]", out, flags=re.I)
    out = _AFFINITY.sub("[AFFILIATION]", out)
    out = _INLINE_ATTR.sub("[REDACTED]", out)
    out = PHONE.sub(lambda m: m.group(0) if _YEAR_RANGE.match(m.group(0).strip()) else "[CONTACT]", out)
    out = URL.sub("[CONTACT]", out)
    out = _INSTITUTION.sub("[INSTITUTION]", out)
    out = _INSTITUTION_NAMED.sub("[INSTITUTION]", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def has_protected_attributes(text: str) -> bool:
    return bool(
        _DROP.search(text) or EMAIL.search(text) or _INSTITUTION.search(text)
        or _INSTITUTION_NAMED.search(text) or _AFFINITY.search(text) or _INLINE_ATTR.search(text)
    )
