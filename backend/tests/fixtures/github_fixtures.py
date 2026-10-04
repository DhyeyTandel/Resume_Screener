"""Recorded GitHub API responses for tests. No live network calls in CI."""
from __future__ import annotations

import base64

REPOS = [
    {"name": "ledger-service", "fork": False, "created_at": "2022-01-01T00:00:00Z",
     "pushed_at": "2024-06-01T00:00:00Z", "topics": ["fastapi", "postgresql"],
     "default_branch": "main", "language": "Python",
     "description": "Settlement ledger service"},
    # A fork's pushed_at is the upstream's last push, i.e. BEFORE the fork was created, when the
    # candidate never committed to it (this is what the listing-only fork check relies on).
    {"name": "old-tutorial-clone", "fork": True, "created_at": "2020-01-02T00:00:00Z",
     "pushed_at": "2020-01-01T00:00:00Z", "topics": [], "default_branch": "main",
     "language": "JavaScript",
     "description": "Following along with a freecodecamp tutorial project."},
]
LANGUAGES = {
    "ledger-service": {"Python": 40000},
    "old-tutorial-clone": {"JavaScript": 5000},
}
COMMITS = {
    "ledger-service": [
        {"author": {"login": "priya"}, "commit": {"author": {"email": "priya@example.com"}}}
        for _ in range(18)
    ] + [{"author": {"login": "someone-else"}, "commit": {"author": {"email": "x@x.com"}}}
         for _ in range(2)],
    "old-tutorial-clone": [],  # candidate never committed to their own fork
}
MANIFESTS = {"ledger-service": {"requirements.txt", "Dockerfile"}, "old-tutorial-clone": set()}
README = {
    "ledger-service": "A FastAPI + PostgreSQL settlement ledger service.",
    "old-tutorial-clone": "Following along with a freecodecamp tutorial project.",
}
# Extra tree paths per repo (tests dir, CI), beyond the manifests above.
EXTRA_PATHS = {"ledger-service": ["src", "src/main.py"], "old-tutorial-clone": []}


def _tree(name: str, manifests: dict, extra: dict) -> list[dict]:
    paths = sorted(manifests.get(name, set())) + list(extra.get(name, []))
    return [{"path": p, "type": "blob" if "." in p.rsplit("/", 1)[-1] else "tree"} for p in paths]


def make_world(
    repos: list[dict] | None = None, *, languages: dict | None = None, commits: dict | None = None,
    manifests: dict | None = None, readme: dict | None = None, extra_paths: dict | None = None,
    manifest_contents: dict | None = None,
) -> dict:
    return {
        "repos": REPOS if repos is None else repos,
        "languages": LANGUAGES if languages is None else languages,
        "commits": COMMITS if commits is None else commits,
        "manifests": MANIFESTS if manifests is None else manifests,
        "readme": README if readme is None else readme,
        "extra_paths": EXTRA_PATHS if extra_paths is None else extra_paths,
        # repo name -> {manifest file name: text}. Absent files answer 404, like an unreadable one.
        "manifest_contents": manifest_contents or {},
    }


def _serve(world: dict, username: str, url: str, trees_truncated: set[str]) -> tuple[int, object]:
    if url.endswith("/repos?per_page=100&sort=pushed"):
        return 200, world["repos"]
    for repo in world["repos"]:
        name = repo["name"]
        base = f"/repos/{username}/{name}"
        if url.endswith(f"{base}/languages"):
            return 200, world["languages"].get(name, {})
        if url.endswith(f"{base}/readme"):
            if name not in world["readme"]:
                return 404, {}
            return 200, {"content": base64.b64encode(world["readme"][name].encode()).decode()}
        if url.endswith(f"{base}/commits?per_page=100"):
            return 200, world["commits"].get(name, [])
        if f"{base}/contents/" in url:
            fname = url.split(f"{base}/contents/", 1)[1]
            text = world.get("manifest_contents", {}).get(name, {}).get(fname)
            if text is None:
                return 404, {}
            return 200, {"content": base64.b64encode(text.encode()).decode()}
        if f"{base}/git/trees/" in url:
            full = _tree(name, world["manifests"], world["extra_paths"])
            if url.endswith("?recursive=1"):
                if name in trees_truncated:
                    return 200, {"tree": full[:1], "truncated": True}
                return 200, {"tree": full, "truncated": False}
            return 200, {"tree": [e for e in full if "/" not in e["path"]], "truncated": False}
    return 404, {}


def make_fetch(username: str = "priya", *, not_found: bool = False, rate_limited: bool = False,
               world: dict | None = None, trees_truncated: set[str] | None = None):
    """Returns a `Fetch` callable that replays the fixtures above, as (status, body).

    `fetch.calls` lists every URL requested, so tests can count requests."""
    world = world or make_world()
    calls: list[str] = []

    async def fetch(url: str, headers: dict | None):
        calls.append(url)
        if not_found:
            return 404, {}
        if rate_limited:
            return 403, {}
        return _serve(world, username, url, trees_truncated or set())

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def make_fetch_with_headers(
    username: str = "priya", *, world: dict | None = None, remaining_start: int | None = None,
    rate_limit_after: int | None = None, rate_limit_on: str | None = None,
    trees_truncated: set[str] | None = None, raise_on: str | None = None,
):
    """Backwards-compatible variant of make_fetch returning (status, body, headers).

    - remaining_start: X-RateLimit-Remaining on the first reply, counting down by one per call.
    - rate_limit_on: a URL substring; the first request containing it, and every request
      after it, gets 403 with X-RateLimit-Remaining: 0 (a real unauthenticated limit).
    - rate_limit_after: the same, but after N successful requests.
    - raise_on: a URL substring that raises a transport error.
    """
    world = world or make_world()
    calls: list[str] = []
    state = {"limited": False}

    async def fetch(url: str, headers: dict | None):
        calls.append(url)
        if raise_on and raise_on in url:
            raise ConnectionError("simulated transport failure")
        if rate_limit_on and rate_limit_on in url:
            state["limited"] = True
        if rate_limit_after is not None and len(calls) > rate_limit_after:
            state["limited"] = True
        if state["limited"]:
            return 403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0"}
        status, body = _serve(world, username, url, trees_truncated or set())
        hdrs = {}
        if remaining_start is not None:
            hdrs["X-RateLimit-Remaining"] = str(max(remaining_start - len(calls) + 1, 0))
        return status, body, hdrs

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch
