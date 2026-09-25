"""Recorded GitHub API responses for tests. No live network calls in CI."""
from __future__ import annotations

REPOS = [
    {"name": "ledger-service", "fork": False, "created_at": "2022-01-01T00:00:00Z",
     "pushed_at": "2024-06-01T00:00:00Z", "topics": ["fastapi", "postgresql"]},
    {"name": "old-tutorial-clone", "fork": True, "created_at": "2020-01-01T00:00:00Z",
     "pushed_at": "2020-01-02T00:00:00Z", "topics": []},
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


def make_fetch(username: str = "priya", *, not_found: bool = False, rate_limited: bool = False):
    """Returns a `Fetch` callable that replays the fixtures above."""
    import base64

    async def fetch(url: str, headers: dict | None):
        if not_found:
            return 404, {}
        if rate_limited:
            return 403, {}
        if url.endswith("/repos?per_page=100&sort=pushed"):
            return 200, REPOS
        for name in LANGUAGES:
            if url.endswith(f"/repos/{username}/{name}/languages"):
                return 200, LANGUAGES[name]
            if url.endswith(f"/repos/{username}/{name}/readme"):
                content = base64.b64encode(README[name].encode()).decode()
                return 200, {"content": content}
            if url.endswith(f"/repos/{username}/{name}/commits?per_page=100"):
                return 200, COMMITS[name]
            for manifest in ("requirements.txt", "pyproject.toml", "package.json", "pom.xml",
                             "go.mod", "Dockerfile"):
                if url.endswith(f"/repos/{username}/{name}/contents/{manifest}"):
                    return (200, {}) if manifest in MANIFESTS[name] else (404, {})
            if url.endswith(f"/repos/{username}/{name}/contents/.github/workflows"):
                return 404, {}
            for testdir in ("tests", "test"):
                if url.endswith(f"/repos/{username}/{name}/contents/{testdir}"):
                    return 404, {}
        return 404, {}

    return fetch
