"""Module B Stage 4: deterministic consistency checks, no LLM (Spec 11 Stage 4).

Implements:
- technology anachronism (a claimed year of experience with a technology
  exceeding the years since its public release)
- overlapping full-time roles (from the resume alone - no second source needed)
- date conflicts and title/employer mismatch between the resume and a
  candidate-provided LinkedIn export, when one is collected
"""
from __future__ import annotations

import difflib
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

from .collectors.linkedin import LinkedInEvidence

_PATH = Path(__file__).with_name("data") / "tech_release_dates.yaml"
_YEARS_PREFIX = r"(\d+)\s*\+?\s*years?\s+(?:of\s+|with\s+)?(?:[a-z][a-z0-9+#.]*\s+){0,2}"


@lru_cache(maxsize=1)
def release_years() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _PATH.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        try:
            out[key.strip().lower()] = int(val.strip())
        except ValueError:
            continue
    return out


def check_anachronisms(resume_text: str, *, tolerance_years: int = 0) -> list[dict]:
    """'5 years of FastAPI' when FastAPI is 3 years old -> a contradiction."""
    years_db = release_years()
    today_year = date.today().year
    out = []
    for name, released in years_db.items():
        pattern = re.compile(_YEARS_PREFIX + re.escape(name), re.I)
        for m in pattern.finditer(resume_text):
            claimed = int(m.group(1))
            available = today_year - released
            if claimed > available + tolerance_years:
                out.append(
                    {
                        "type": "technology_anachronism",
                        "detail": (
                            f"'{m.group(0).strip()}' claims {claimed} years, but "
                            f"{name.title()} has existed for about {available} years."
                        ),
                        "sources": ["resume"],
                    }
                )
    return out


def _parse_year(y: str, *, is_end: bool = False) -> int | None:
    y = (y or "").strip().lower()
    if y in ("present", "current", ""):
        return date.today().year if is_end else None
    try:
        return int(y)
    except ValueError:
        return None


def check_role_overlap(experience: list[dict], *, tolerance_months: int = 2) -> list[dict]:
    """Two full-time roles claimed at the same time is a contradiction the
    resume alone can reveal - no second source needed."""
    tolerance_years = tolerance_months / 12
    spans = []
    for e in experience:
        start = _parse_year(e.get("start", ""))
        end = _parse_year(e.get("end", ""), is_end=True)
        if start is not None and end is not None and end >= start:
            display_end = "Present" if str(e.get("end", "")).lower() in ("present", "current") else e.get("end", end)
            spans.append((start, end, display_end, e))
    out = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            s1, e1, disp1, r1 = spans[i]
            s2, e2, disp2, r2 = spans[j]
            overlap = min(e1, e2) - max(s1, s2)
            if overlap > tolerance_years:
                out.append(
                    {
                        "type": "overlapping_roles",
                        "detail": (
                            f"'{r1.get('title') or 'a role'}' ({s1}-{disp1}) and "
                            f"'{r2.get('title') or 'a role'}' ({s2}-{disp2}) overlap by more than "
                            f"{tolerance_months} months, both shown as full-time."
                        ),
                        "sources": ["resume"],
                    }
                )
    return out


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# --- Title normalisation (Spec 11 Stage 4: title/employer mismatch) -------------------------
# "Software Engineer" and "Software Developer" are the same job under two names, so they must
# not be reported as a mismatch. Known equivalents are collapsed before comparing.
_PHRASE_EQUIV = (
    (re.compile(r"\bfront\s+end\b"), "frontend"),
    (re.compile(r"\bback\s+end\b"), "backend"),
    (re.compile(r"\bfull\s+stack\b"), "fullstack"),
    (re.compile(r"\bdev\s+ops\b"), "devops"),
    (re.compile(r"\b(?:swe|sde)\b"), "software engineer"),
)
_WORD_EQUIV = {
    "engineers": "engineer", "engineering": "engineer", "developer": "engineer",
    "developers": "engineer", "programmer": "engineer", "dev": "engineer", "eng": "engineer",
    "sr": "senior", "jr": "junior", "mgr": "manager", "managers": "manager",
}
_TITLE_NOISE = {"of", "the", "and", "a", "an", "at"}

# Seniority ranks, low to high. Only a HIGHER rank on the resume than on LinkedIn is flagged.
_RANK = {"senior": 1, "lead": 2, "staff": 2, "manager": 2, "principal": 3, "head": 3, "director": 4}


def normalise_title(title: str) -> list[str]:
    """Lower-cased tokens with known synonyms collapsed (engineer/developer/programmer,
    SWE -> software engineer, sr -> senior, jr -> junior, front end -> frontend, ...)."""
    t = re.sub(r"[^a-z0-9+#]+", " ", (title or "").lower()).strip()
    for pat, repl in _PHRASE_EQUIV:
        t = pat.sub(repl, t)
    out = []
    for w in t.split():
        w = _WORD_EQUIV.get(w, w)
        out.extend(w.split())  # a phrase replacement can add a space
        if w in _TITLE_NOISE:
            out.pop()
    return out


def title_rank(tokens: list[str]) -> int:
    """Highest seniority rank named in the title, 0 when none."""
    return max((_RANK[w] for w in tokens if w in _RANK), default=0)


def compare_titles(resume_title: str, linkedin_title: str, *, threshold: int = 80) -> str | None:
    """None when the titles agree; 'different_role' for a genuinely different title;
    'seniority_inflation' when the resume adds a higher rank that LinkedIn lacks.

    A LOWER rank on the resume than on LinkedIn is not a conflict: people routinely list the
    title they were hired into, so only the upward direction is worth a neutral follow-up.
    Rank words are stripped for the role comparison so 'Principal Data Engineer' vs 'Data
    Engineer' is read as the same job at a different level, not as a different job."""
    r_tokens, l_tokens = normalise_title(resume_title), normalise_title(linkedin_title)
    r_rank, l_rank = title_rank(r_tokens), title_rank(l_tokens)
    full = _ratio(" ".join(r_tokens), " ".join(l_tokens)) * 100
    r_base = [w for w in r_tokens if w not in _RANK]
    l_base = [w for w in l_tokens if w not in _RANK]
    base = _ratio(" ".join(r_base), " ".join(l_base)) * 100 if r_base and l_base else full
    same_role = full >= threshold or base >= threshold
    if not same_role:
        return "different_role"
    if r_rank > l_rank:
        return "seniority_inflation"
    return None


def check_linkedin_consistency(
    resume_experience: list[dict], li: LinkedInEvidence, *, date_tolerance_months: int = 2,
    title_threshold: int = 80, company_threshold: int = 60,
) -> list[dict]:
    """Date conflicts and title/employer mismatch, resume vs LinkedIn export.

    Roles are paired by EMPLOYER first, not by title: matching the closest
    title across different companies produced false date-conflicts between
    two genuinely different jobs (caught live against the real GitHub/
    LinkedIn-shaped data, not a hypothetical - see ASSUMPTIONS.md). A resume
    role with no company match on LinkedIn is left uncompared, not flagged -
    a role LinkedIn simply doesn't list is missing evidence, never a
    contradiction (Spec 2.4).

    Each finding also carries `resume_title`, `company` and `citation` so the engine can
    mark the matching ROLE claim CONTRADICTED (a source conflicts with the claim, Stage 3).
    """
    if li.status != "ok" or not li.roles:
        return []
    out = []
    tol_years = date_tolerance_months / 12
    for r in resume_experience:
        r_title = (r.get("title") or "").strip().lower()
        r_company = (r.get("company") or "").strip().lower()
        if not r_title or not r_company:
            continue  # can't pair by employer without one
        best = max(
            li.roles, key=lambda lr: _ratio(r_company, lr.company.lower()), default=None
        )
        if best is None or _ratio(r_company, best.company.lower()) * 100 < company_threshold:
            continue  # no corresponding employer on LinkedIn - not comparable, not a finding

        ref = {
            "resume_title": r.get("title"), "company": r.get("company"),
            "citation": f"linkedin_export:role:{best.title}",
        }
        verdict = compare_titles(r.get("title") or "", best.title, threshold=title_threshold)
        if verdict == "different_role":
            out.append(
                {
                    "type": "title_mismatch",
                    "detail": f"Resume title '{r.get('title')}' does not match the LinkedIn "
                    f"title '{best.title}' for the same employer.",
                    "sources": ["resume", "linkedin"], **ref,
                }
            )
            continue
        if verdict == "seniority_inflation":
            out.append(
                {
                    "type": "title_mismatch",
                    "detail": f"Resume title '{r.get('title')}' names a more senior level than "
                    f"the LinkedIn title '{best.title}' for the same employer. Titles are "
                    "sometimes updated on only one profile; worth confirming.",
                    "sources": ["resume", "linkedin"], **ref,
                }
            )
            continue
        r_start, r_end = _parse_year(r.get("start", "")), _parse_year(r.get("end", ""), is_end=True)
        li_start, li_end = _parse_year(best.start), _parse_year(best.end, is_end=True)
        if None not in (r_start, r_end, li_start, li_end):
            if abs(r_start - li_start) > tol_years or abs(r_end - li_end) > tol_years:  # type: ignore[operator]  # None excluded by the `None not in (...)` guard
                out.append(
                    {
                        "type": "date_conflict",
                        "detail": f"'{r.get('title')}' is dated {r_start}-{r.get('end')} on the "
                        f"resume but {li_start}-{best.end} on LinkedIn.",
                        "sources": ["resume", "linkedin"], **ref,
                    }
                )
    return out


_DEGREE_WORD = re.compile(
    r"\b(bachelor\w*|b\.?\s?s\.?c?|b\.?\s?tech|b\.?\s?e|master\w*|m\.?\s?s\.?c?|m\.?\s?tech|"
    r"mba|ph\.?\s?d|doctorate)\b",
    re.I,
)
_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20\d\d)(?!\d)")
_SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|lead|principal|staff|manager)\b", re.I)
_EXEMPT_TITLE = re.compile(
    r"\b(intern\w*|part[- ]?time|student|teaching\s+assistant|research\s+assistant|"
    r"junior|jr\.?|trainee|apprentice|co-?op)\b",
    re.I,
)


def graduation_year(education: list[str]) -> int | None:
    """Latest plausible year on a line naming a degree; None when absent."""
    this_year = date.today().year
    years = []
    for line in education or []:
        if not _DEGREE_WORD.search(line):
            continue
        years += [int(y) for y in _YEAR.findall(line) if int(y) <= this_year]
    return max(years) if years else None


def check_graduation_consistency(
    education: list[str], experience: list[dict], role_level: str | None = None
) -> list[dict]:
    """Flag only a clear mismatch: a senior-level full-time role that starts
    more than a year before the stated graduation year. Working while studying
    is normal, so ordinary, intern, part-time or junior roles are never flagged.
    Missing data yields no finding."""
    grad = graduation_year(education)
    if grad is None:
        return []
    out = []
    for e in experience:
        title = (e.get("title") or "").strip()
        start = _parse_year(e.get("start", ""))
        if not title or start is None:
            continue
        if _EXEMPT_TITLE.search(title) or not _SENIOR_TITLE.search(title):
            continue
        if start < grad - 1:
            out.append(
                {
                    "type": "graduation_inconsistency",
                    "detail": (
                        f"'{title}' is shown as starting in {start}, while the education "
                        f"section lists a graduation year of {grad}. The dates may reflect "
                        "work alongside study or an earlier qualification; worth confirming."
                    ),
                    "sources": ["resume"],
                }
            )
    return out
