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


def check_linkedin_consistency(
    resume_experience: list[dict], li: LinkedInEvidence, *, date_tolerance_months: int = 2,
    title_threshold: int = 80,
) -> list[dict]:
    """Date conflicts and title/employer mismatch, resume vs LinkedIn export."""
    if li.status != "ok" or not li.roles:
        return []
    out = []
    tol_years = date_tolerance_months / 12
    for r in resume_experience:
        r_title = (r.get("title") or "").strip().lower()
        r_company = (r.get("company") or "").strip().lower()
        if not r_title:
            continue
        best = max(
            li.roles,
            key=lambda lr: difflib.SequenceMatcher(None, r_title, lr.title.lower()).ratio(),
            default=None,
        )
        if best is None:
            continue
        ratio = difflib.SequenceMatcher(None, r_title, best.title.lower()).ratio()
        if ratio * 100 < title_threshold and r_company and best.company:
            company_ratio = difflib.SequenceMatcher(None, r_company, best.company.lower()).ratio()
            if company_ratio * 100 < title_threshold:
                continue  # not the same role at all - nothing to compare
            out.append(
                {
                    "type": "title_mismatch",
                    "detail": f"Resume title '{r.get('title')}' does not match the LinkedIn "
                    f"title '{best.title}' for the same employer.",
                    "sources": ["resume", "linkedin"],
                }
            )
            continue
        r_start, r_end = _parse_year(r.get("start", "")), _parse_year(r.get("end", ""), is_end=True)
        li_start, li_end = _parse_year(best.start), _parse_year(best.end, is_end=True)
        if None not in (r_start, r_end, li_start, li_end):
            if abs(r_start - li_start) > tol_years or abs(r_end - li_end) > tol_years:
                out.append(
                    {
                        "type": "date_conflict",
                        "detail": f"'{r.get('title')}' is dated {r_start}-{r.get('end')} on the "
                        f"resume but {li_start}-{best.end} on LinkedIn.",
                        "sources": ["resume", "linkedin"],
                    }
                )
    return out
