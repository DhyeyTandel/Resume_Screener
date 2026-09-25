"""Module B Stage 4: deterministic consistency checks, no LLM (Spec 11 Stage 4).

This slice implements technology anachronism: a claimed year of experience with
a technology exceeding the years since its public release. Date-conflict and
title/employer-mismatch checks need a second source (LinkedIn) that this build
does not collect (Assumption A-6), so they are not implemented; consistency
degrades to 1.0 (no checks performed => no contradictions found) as before.
"""
from __future__ import annotations
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

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
