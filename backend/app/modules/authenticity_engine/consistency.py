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
                        "technology": name,
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


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH_YEAR = re.compile(r"^([a-z]{3,9})\.?,?\s+(\d{4})$")  # "Jan 2020", "September 2021"
_NUM_MONTH_YEAR = re.compile(r"^(\d{1,2})\s*[/.-]\s*(\d{4})$")  # "01/2020"
_YEAR_NUM_MONTH = re.compile(r"^(\d{4})\s*[/.-]\s*(\d{1,2})$")  # "2020-01"

# Roles that are not a full-time job. Holding one alongside a main job is ordinary (a student
# tutoring, a freelancer with a day job, a volunteer), so such a role never counts as "a second
# full-time role" (Spec 2.4: absence of full-time proof is not a contradiction; fairness).
_NOT_FULL_TIME = re.compile(
    r"\b(intern\w*|part[- ]?time|student|"
    r"(?:teaching|research|graduate|lab)\s+(?:assistant|fellow)|assistant\s+(?:lecturer|instructor)|"
    r"ta|ra|co-?op|apprentice\w*|freelanc\w*|contract(?:or|ing)?|volunteer\w*|tutor\w*|"
    r"mentor\w*|adjunct|seasonal|casual|per[- ]?diem|gig|side\s+(?:project|gig|hustle)|"
    r"moonlight\w*|self[- ]employed)\b",
    re.I,
)


def is_full_time(role: dict) -> bool:
    """False when the title, company or an explicit employment-type field marks the role as
    part-time, an internship, freelance, contract, volunteer, tutoring or similar."""
    text = " ".join(
        str(role.get(k) or "") for k in ("title", "company", "employment_type", "type")
    )
    return not _NOT_FULL_TIME.search(text)


def _parse_ym(s: str) -> tuple[int, int | None] | None:
    """(year, month or None). 'Present' resolves to today. None when unparseable."""
    s = str(s or "").strip().lower()
    if s in ("present", "current", "now", "ongoing", "to date"):
        today = date.today()
        return today.year, today.month
    if re.fullmatch(r"\d{4}", s):
        return int(s), None
    if m := _MONTH_YEAR.match(s):
        mon = _MONTHS.get(m.group(1)[:3])
        return (int(m.group(2)), mon) if mon else None
    if m := _NUM_MONTH_YEAR.match(s):
        return (int(m.group(2)), int(m.group(1))) if 1 <= int(m.group(1)) <= 12 else None
    if m := _YEAR_NUM_MONTH.match(s):
        return (int(m.group(1)), int(m.group(2))) if 1 <= int(m.group(2)) <= 12 else None
    return None


def _endpoint(role: dict, which: str) -> tuple[int, int | None] | None:
    """(year, month) of a role's 'start' or 'end'. The month comes from the date string
    ('Jan 2020', '01/2020') or, for a year string, from the structured `<which>_month` field
    the resume parser fills in."""
    ym = _parse_ym(role.get(which, ""))
    if ym is not None and ym[1] is None:
        m = role.get(f"{which}_month")
        if isinstance(m, int) and 1 <= m <= 12:
            return ym[0], m
    return ym


def _shown(role: dict, which: str) -> str:
    ym = _endpoint(role, which)
    if ym is not None and ym[1] is not None and re.fullmatch(r"\d{4}", str(role.get(which) or "")):
        return f"{ym[1]:02d}/{ym[0]}"
    return str(role.get(which) or "")


def check_role_overlap(experience: list[dict], *, tolerance_months: int = 2) -> list[dict]:
    """Two FULL-TIME roles claimed at the same time is an inconsistency the resume alone can
    reveal - no second source needed. Part-time, intern, freelance, contract, volunteer, tutor,
    TA/RA and similar roles are exempt (see `is_full_time`). When both roles carry months the
    overlap is measured in months; otherwise at year granularity, as before.

    The finding is resume-internal: it does not say which entry is wrong and no second source
    disagrees, so the engine reports it and asks about it but never lets it set the band."""
    tolerance_years = tolerance_months / 12
    spans = []
    for e in experience:
        if not is_full_time(e):
            continue
        a, b = _endpoint(e, "start"), _endpoint(e, "end")
        if a is None or b is None or str(e.get("start") or "").strip() == "":
            continue
        if (b[0], b[1] or 12) < (a[0], a[1] or 1):
            continue
        spans.append((a, b, e))
    out = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            (a1, b1, r1), (a2, b2, r2) = spans[i], spans[j]
            if all(x[1] is not None for x in (a1, b1, a2, b2)):
                # Month granularity: month index = year*12 + month.
                idx = lambda ym: ym[0] * 12 + (ym[1] or 1)  # noqa: E731
                overlap = min(idx(b1), idx(b2)) - max(idx(a1), idx(a2))
                over = overlap > tolerance_months
            else:
                overlap = min(b1[0], b2[0]) - max(a1[0], a2[0])  # years, as before
                over = overlap > tolerance_years
            if over:
                out.append(
                    {
                        "type": "overlapping_roles",
                        "detail": (
                            f"'{r1.get('title') or 'a role'}' ({_shown(r1, 'start')}-{_shown(r1, 'end')}) and "
                            f"'{r2.get('title') or 'a role'}' ({_shown(r2, 'start')}-{_shown(r2, 'end')}) "
                            f"overlap by more than {tolerance_months} months, and neither is "
                            "labelled part-time, internship, freelance, contract or similar. "
                            "Both may be correct (for example a concurrent engagement); worth a "
                            "question rather than a conclusion."
                        ),
                        "sources": ["resume"],
                        "titles": [r1.get("title"), r2.get("title")],
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
    r"\b(junior|jr\.?|trainee)\b|" + _NOT_FULL_TIME.pattern,
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
                    "sources": ["resume"], "title": title,
                }
            )
    return out
