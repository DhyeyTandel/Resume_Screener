"""Fake fetch functions built from a candidate's synthetic world. No network, ever.

The GitHub fake replays the response shapes of backend/tests/fixtures/github_fixtures.py (repo listing,
languages, readme, commits, git trees) by reusing its `make_world` and `make_fetch`. The portfolio fake
serves robots.txt, the home page and the demo links, like portfolio_fixtures.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from fixtures.github_fixtures import make_fetch, make_world  # noqa: E402


def github_fetch(gh: dict | None):
    """Returns (username, fetch) for a candidate's GitHub world, or (None, None) when absent."""
    if not gh:
        return None, None
    user = gh["username"]
    if gh["status"] == "not_found":
        return user, make_fetch(user, not_found=True)
    repos, languages, commits, manifests, readme, extra = [], {}, {}, {}, {}, {}
    for r in gh["repos"]:
        raw = {k: r[k] for k in ("name", "fork", "created_at", "pushed_at", "topics", "default_branch",
                                 "language", "description")}
        repos.append(raw)
        languages[r["name"]] = r["languages"]
        n_mine, n_other = r["commits"]["mine"], r["commits"]["others"]
        commits[r["name"]] = (
            [{"author": {"login": user}, "commit": {"author": {"email": f"{user}@example.com"}}}] * n_mine
            + [{"author": {"login": "teammate"}, "commit": {"author": {"email": "t@example.com"}}}] * n_other
        )
        manifests[r["name"]] = set(r["manifests"])
        if r["readme"] is not None:
            readme[r["name"]] = r["readme"]
        extra[r["name"]] = list(r["extra_paths"])
    world = make_world(repos, languages=languages, commits=commits, manifests=manifests, readme=readme,
                       extra_paths=extra)
    return user, make_fetch(user, world=world)


def portfolio_fetch(pf: dict | None):
    """Returns (url, fetch) for a candidate's portfolio world, or (None, None)."""
    if not pf:
        return None, None
    url = pf["url"]
    host = url.split("://", 1)[1]
    robots = "User-agent: *\nDisallow: /\n" if pf["robots"] == "disallow" else "User-agent: *\nDisallow:\n"

    async def fetch(u: str) -> tuple[int, str]:
        if u.endswith("/robots.txt"):
            return 200, robots
        if "-demo.vercel.app" in u:
            return (200 if pf["demo_ok"] else 503), ("<html>demo</html>" if pf["demo_ok"] else "")
        if host in u:
            return 200, pf["html"]
        return 404, ""

    return url, fetch


def sources(cand: dict, *, world_override: dict | None = None) -> dict:
    """Keyword arguments for screen_candidate built from the candidate's world."""
    world = dict(cand["world"])
    if world_override:
        world.update(world_override)
    user, gh_fetch = github_fetch(world.get("github"))
    url, pf_fetch = portfolio_fetch(world.get("portfolio"))
    return {
        "github_username": user, "github_fetch": gh_fetch,
        "portfolio_url": url, "portfolio_fetch": pf_fetch,
        "linkedin_export": world.get("linkedin"),
    }
