"""GitHub evidence collector (Module B Stage 2, public REST, optional token).

Testability: `fetch` is injectable so tests can replay recorded fixtures
instead of hitting the network (Spec: no live network calls in CI).
"""
from __future__ import annotations
import base64
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ....config import cfg

Fetch = Callable[[str, dict | None], Awaitable[tuple[int, dict | list]]]

MANIFESTS = (
    "requirements.txt", "pyproject.toml", "package.json", "pom.xml", "go.mod", "Dockerfile",
)
TUTORIAL_MARKERS = re.compile(
    r"\b(tutorial|freecodecamp|udemy|coursera|bootcamp[- ]?project|clone[- ]?coding|"
    r"following along|exercise \d+|starter code)\b", re.I,
)


@dataclass
class RepoEvidence:
    name: str
    owned: bool
    fork: bool
    languages: dict[str, int] = field(default_factory=dict)
    created_at: str = ""
    pushed_at: str = ""
    topics: list[str] = field(default_factory=list)
    readme_excerpt: str = ""
    manifests_found: list[str] = field(default_factory=list)
    commits_by_candidate: int = 0
    commits_total: int = 0
    lines_by_candidate: int = 0
    lines_total: int = 0
    has_tests: bool = False
    has_ci: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def authorship_ratio(self) -> float:
        return round(self.commits_by_candidate / self.commits_total, 3) if self.commits_total else 0.0

    @property
    def line_share(self) -> float:
        return round(self.lines_by_candidate / self.lines_total, 3) if self.lines_total else 0.0


@dataclass
class GitHubEvidence:
    username: str
    status: str  # ok | missing | error
    repos: list[RepoEvidence] = field(default_factory=list)
    error: str | None = None


async def _default_fetch(url: str, headers: dict | None) -> tuple[int, dict | list]:
    import httpx

    async with httpx.AsyncClient(timeout=float(cfg("http.timeout_s", 10))) as client:
        resp = await client.get(url, headers=headers or {})
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - non-JSON body (e.g. raw README)
            body = {"_text": resp.text}
        return resp.status_code, body


async def collect_github(
    username: str | None,
    *,
    candidate_email_hints: list[str] | None = None,
    token: str | None = None,
    fetch: Fetch | None = None,
) -> GitHubEvidence:
    """Stage 2 GitHub collector. Never raises: failures become status='error'."""
    if not username:
        return GitHubEvidence(username="", status="missing")

    fetch = fetch or _default_fetch
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    max_repos = int(cfg("authenticity.github.max_repos", 30))
    max_commits = int(cfg("authenticity.github.max_commits_per_repo", 300))

    status, repos_raw = await fetch(
        f"https://api.github.com/users/{username}/repos?per_page=100&sort=pushed", headers
    )
    if status == 404:
        return GitHubEvidence(username=username, status="error", error="GitHub user not found")
    if status == 403:
        return GitHubEvidence(username=username, status="error", error="GitHub rate limit reached")
    if status != 200 or not isinstance(repos_raw, list):
        return GitHubEvidence(username=username, status="error", error=f"unexpected status {status}")

    repos: list[RepoEvidence] = []
    for raw in repos_raw[:max_repos]:
        name = raw.get("name", "")
        fork = bool(raw.get("fork"))
        ev = RepoEvidence(
            name=name,
            owned=not fork,
            fork=fork,
            created_at=raw.get("created_at", ""),
            pushed_at=raw.get("pushed_at", ""),
            topics=raw.get("topics", []) or [],
        )

        lang_status, langs = await fetch(f"https://api.github.com/repos/{username}/{name}/languages", headers)
        if lang_status == 200 and isinstance(langs, dict):
            ev.languages = langs

        readme_status, readme = await fetch(f"https://api.github.com/repos/{username}/{name}/readme", headers)
        if readme_status == 200 and isinstance(readme, dict) and readme.get("content"):
            try:
                ev.readme_excerpt = base64.b64decode(readme["content"]).decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001
                pass
        if ev.readme_excerpt and TUTORIAL_MARKERS.search(ev.readme_excerpt):
            ev.flags.append("tutorial_clone")

        for manifest in MANIFESTS:
            m_status, _ = await fetch(f"https://api.github.com/repos/{username}/{name}/contents/{manifest}", headers)
            if m_status == 200:
                ev.manifests_found.append(manifest)
        ci_status, _ = await fetch(f"https://api.github.com/repos/{username}/{name}/contents/.github/workflows", headers)
        ev.has_ci = ci_status == 200
        for testdir in ("tests", "test"):
            t_status, _ = await fetch(f"https://api.github.com/repos/{username}/{name}/contents/{testdir}", headers)
            if t_status == 200:
                ev.has_tests = True
                break

        c_status, commits = await fetch(
            f"https://api.github.com/repos/{username}/{name}/commits?per_page={min(100, max_commits)}", headers
        )
        if c_status == 200 and isinstance(commits, list):
            ev.commits_total = len(commits)
            for c in commits:
                author = (c.get("author") or {}).get("login")
                commit_email = ((c.get("commit") or {}).get("author") or {}).get("email", "")
                if author == username or (candidate_email_hints and commit_email in candidate_email_hints):
                    ev.commits_by_candidate += 1
            ev.lines_total = 1  # placeholder without per-commit stat calls (rate-limit budget)
            ev.lines_by_candidate = 1 if ev.commits_by_candidate else 0

        threshold = float(cfg("authenticity.github.bulk_import_ratio", 0.80))
        if ev.commits_total and ev.commits_total <= 2 and ev.line_share >= threshold:
            ev.flags.append("bulk_import")
        if fork and ev.commits_by_candidate == 0:
            ev.flags.append("fork_claimed_as_own")

        repos.append(ev)

    return GitHubEvidence(username=username, status="ok", repos=repos)
