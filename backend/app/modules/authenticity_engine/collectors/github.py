"""GitHub evidence collector (Module B Stage 2, public REST, optional token).

Testability: `fetch` is injectable so tests can replay recorded fixtures
instead of hitting the network (Spec: no live network calls in CI).
"""
from __future__ import annotations

import base64
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ....config import cfg
from ....db.cache import cache_enabled, cached_fetch

# A fetch returns (status, body) or (status, body, response_headers). Headers are optional so
# older injected fetches keep working; when present, X-RateLimit-Remaining is honoured.
Fetch = Callable[[str, dict | None], Awaitable[tuple]]

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
    default_branch: str = ""
    description: str = ""
    primary_language: str = ""
    # analyzed: deep per-repo calls were made. Forks (and repos skipped by the caps) are not.
    analyzed: bool = False
    # data_complete False means a check on this repo did not finish (rate limit, transport
    # error), so absence of evidence here is NOT evidence of absence (Spec 2.4).
    data_complete: bool = True
    incomplete_reason: str = ""
    tree_truncated: bool = False

    @property
    def authorship_ratio(self) -> float:
        return round(self.commits_by_candidate / self.commits_total, 3) if self.commits_total else 0.0

    @property
    def line_share(self) -> float:
        return round(self.lines_by_candidate / self.lines_total, 3) if self.lines_total else 0.0


@dataclass
class GitHubEvidence:
    username: str
    status: str  # ok | partial | missing | error
    repos: list[RepoEvidence] = field(default_factory=list)
    error: str | None = None
    partial_reason: str | None = None
    requests_made: int = 0
    rate_limit_remaining: int | None = None

    @property
    def has_data(self) -> bool:
        return self.status in ("ok", "partial")

    @property
    def incomplete_repos(self) -> list[RepoEvidence]:
        return [r for r in self.repos if not r.data_complete]


_MANIFEST_SKILLS = {
    "requirements.txt": {"python", "fastapi", "django", "flask"},
    "pyproject.toml": {"python", "fastapi", "django", "flask"},
    "package.json": {"javascript", "typescript", "react", "vue", "node"},
    "pom.xml": {"java"},
    "go.mod": {"go", "golang"},
    "Dockerfile": {"docker"},
}


def repo_covers_skill(repo: RepoEvidence, skill: str) -> bool:
    """True when the collected data of an analyzed repo shows the skill. Forks and repos that
    were skipped are never skill evidence (they have no deep data)."""
    if not repo.analyzed:
        return False
    s = skill.lower()
    if any(s in lang.lower() for lang in repo.languages):
        return True
    if any(s in t.lower() for t in repo.topics):
        return True
    if any(s in _MANIFEST_SKILLS.get(m, set()) for m in repo.manifests_found):
        return True
    return s in repo.readme_excerpt.lower() if repo.readme_excerpt else False


def repo_is_authored(repo: RepoEvidence) -> bool:
    min_auth = float(cfg("authenticity.github.authorship_ratio_min", 0.30))
    return repo.authorship_ratio >= min_auth or (repo.owned and repo.commits_total == 0)


def _listing_relevance(repo: RepoEvidence, skills: list[str]) -> int:
    """How many of the wanted skills the *listing alone* (language, topics, name,
    description) suggests this repo could answer. Used only to order and to size the
    incompleteness of repos we did not open."""
    hay = " ".join([repo.primary_language, repo.name, repo.description, *repo.topics]).lower()
    return sum(1 for s in skills if s.lower() in hay)


class _Incomplete(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _header(headers: dict | None, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return str(v)
    return None


class _Client:
    """Budget- and rate-limit-aware wrapper around an injected/default fetch."""

    def __init__(self, fetch: Fetch, headers: dict):
        self.fetch, self.headers = fetch, headers
        self.requests = 0
        self.remaining: int | None = None
        self.blocked: str | None = None  # once set, no further request is made
        self.reserve = int(cfg("authenticity.github.rate_limit_reserve", 1))
        self.max_requests = int(cfg("authenticity.github.max_requests", 45))

    async def get(self, url: str) -> tuple[int, dict | list]:
        if self.blocked:
            raise _Incomplete(self.blocked)
        if self.remaining is not None and self.remaining <= self.reserve:
            self.blocked = f"GitHub rate limit budget exhausted (X-RateLimit-Remaining={self.remaining})"
            raise _Incomplete(self.blocked)
        if self.requests >= self.max_requests:
            self.blocked = f"request budget of {self.max_requests} per candidate reached"
            raise _Incomplete(self.blocked)
        try:
            res = await self.fetch(url, self.headers)
        except Exception as exc:  # transport error: timeout, DNS, TLS ...
            raise _Incomplete(f"transport error ({type(exc).__name__})") from exc
        self.requests += 1
        status, body = int(res[0]), res[1]
        hdrs = res[2] if len(res) > 2 else None
        rem = _header(hdrs, "x-ratelimit-remaining")
        if rem is not None and rem.strip().lstrip("-").isdigit():
            self.remaining = int(rem)
        message = str(body.get("message", "")).lower() if isinstance(body, dict) else ""
        if status == 429 or (status == 403 and (self.remaining == 0 or "rate limit" in message)):
            self.blocked = "GitHub rate limit reached"
            raise _Incomplete(self.blocked)
        return status, body


async def _default_fetch(url: str, headers: dict | None) -> tuple[int, dict | list, dict]:
    import httpx

    async with httpx.AsyncClient(timeout=float(cfg("http.timeout_s", 10))) as client:
        resp = await client.get(url, headers=headers or {})
        try:
            body = resp.json()
        except Exception:
            body = {"_text": resp.text}
        keep = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit-")}
        return resp.status_code, body, keep


async def _analyze_repo(
    client: _Client, username: str, repo: RepoEvidence, hints: list[str] | None, max_commits: int
) -> None:
    """Deep checks for one repo: tree (manifests/tests/CI), languages, README, commits."""
    repo.analyzed = True
    base = f"https://api.github.com/repos/{username}/{repo.name}"
    reasons: list[str] = []

    async def call(url: str):
        """(found, body) or None when the check could not complete. 404/409 are real answers
        ('no such file', 'empty repository'); anything else non-200 is an incomplete check."""
        try:
            status, body = await client.get(url)
        except _Incomplete as exc:
            if exc.reason not in reasons:
                reasons.append(exc.reason)
            return None
        if status == 200:
            return True, body
        if status in (404, 409):
            return False, {}
        reasons.append(f"HTTP {status}")
        return None

    got = await call(f"{base}/git/trees/{repo.default_branch or 'HEAD'}?recursive=1")
    if got and got[0] and isinstance(got[1], dict):
        entries = list(got[1].get("tree") or [])
        if got[1].get("truncated"):
            # Too big to list recursively: the recursive result can still prove presence,
            # but not absence. Re-read the root (never truncated in practice) for manifests/tests.
            repo.tree_truncated = True
            root = await call(f"{base}/git/trees/{repo.default_branch or 'HEAD'}")
            if root is None:
                pass  # reason recorded; the repo stays incomplete
            elif root[0] and isinstance(root[1], dict):
                entries += list(root[1].get("tree") or [])
        paths = {e.get("path", "") for e in entries if isinstance(e, dict)}
        repo.manifests_found = [m for m in MANIFESTS if m in paths]
        repo.has_tests = any(p in ("tests", "test") or p.startswith(("tests/", "test/")) for p in paths)
        repo.has_ci = any(p.startswith(".github/workflows/") for p in paths)

    if not client.blocked:
        got = await call(f"{base}/languages")
        if got and got[0] and isinstance(got[1], dict):
            repo.languages = got[1]
    if not client.blocked:
        got = await call(f"{base}/readme")
        if got and got[0] and isinstance(got[1], dict) and got[1].get("content"):
            try:
                repo.readme_excerpt = base64.b64decode(got[1]["content"]).decode("utf-8", "replace")[:2000]
            except Exception:
                pass
    if repo.readme_excerpt and TUTORIAL_MARKERS.search(repo.readme_excerpt):
        repo.flags.append("tutorial_clone")
    if not client.blocked:
        got = await call(f"{base}/commits?per_page={min(100, max_commits)}")
        if got and got[0] and isinstance(got[1], list):
            commits = got[1]
            repo.commits_total = len(commits)
            for c in commits:
                author = (c.get("author") or {}).get("login")
                commit_email = ((c.get("commit") or {}).get("author") or {}).get("email", "")
                if author == username or (hints and commit_email in hints):
                    repo.commits_by_candidate += 1
            repo.lines_total = 1  # placeholder without per-commit stat calls (rate-limit budget)
            repo.lines_by_candidate = 1 if repo.commits_by_candidate else 0

    threshold = float(cfg("authenticity.github.bulk_import_ratio", 0.80))
    if repo.commits_total and repo.commits_total <= 2 and repo.line_share >= threshold:
        repo.flags.append("bulk_import")
    if reasons:
        repo.data_complete = False
        repo.incomplete_reason = "; ".join(reasons)


def _stub_from_listing(raw: dict) -> RepoEvidence:
    fork = bool(raw.get("fork"))
    repo = RepoEvidence(
        name=raw.get("name", ""),
        owned=not fork,
        fork=fork,
        created_at=raw.get("created_at", "") or "",
        pushed_at=raw.get("pushed_at", "") or "",
        topics=raw.get("topics", []) or [],
        default_branch=raw.get("default_branch", "") or "",
        description=raw.get("description", "") or "",
        primary_language=raw.get("language", "") or "",
    )
    if fork:
        # Listing-only fork checks (no API calls): a fork whose last push is not after its
        # creation has had no commits of its own; tutorial wording in the name/description.
        if repo.created_at and repo.pushed_at and repo.pushed_at <= repo.created_at:
            repo.flags.append("fork_claimed_as_own")
        if TUTORIAL_MARKERS.search(f"{repo.name} {repo.description}"):
            repo.flags.append("tutorial_clone")
    return repo


def _wanted(skills: list[str] | None) -> list[str]:
    seen: dict[str, str] = {}
    for s in skills or []:
        seen.setdefault(s.lower(), s)
    return list(seen.values())


async def collect_github(
    username: str | None,
    *,
    candidate_email_hints: list[str] | None = None,
    token: str | None = None,
    fetch: Fetch | None = None,
    skills: list[str] | None = None,
) -> GitHubEvidence:
    """Stage 2 GitHub collector. Never raises: failures become status='error', and a rate
    limit or transport error part-way through becomes status='partial' with a reason, with
    the affected repos marked data_complete=False (so matching says UNVERIFIABLE, never
    UNSUPPORTED, for what they could have shown)."""
    if not username:
        return GitHubEvidence(username="", status="missing")

    if fetch is None:
        fetch = cached_fetch(_default_fetch) if cache_enabled() else _default_fetch
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    client = _Client(fetch, headers)

    max_repos = int(cfg("authenticity.github.max_repos", 30))
    max_analyzed = int(cfg("authenticity.github.max_repos_analyzed", 8))
    max_commits = int(cfg("authenticity.github.max_commits_per_repo", 100))

    try:
        status, repos_raw = await client.get(
            f"https://api.github.com/users/{username}/repos?per_page=100&sort=pushed"
        )
    except _Incomplete as exc:
        msg = exc.reason if "rate limit" in exc.reason else f"GitHub could not be reached: {exc.reason}"
        return GitHubEvidence(username=username, status="error", error=msg)
    if status == 404:
        return GitHubEvidence(username=username, status="error", error="GitHub user not found")
    if status == 403:
        return GitHubEvidence(username=username, status="error", error="GitHub rate limit reached")
    if status != 200 or not isinstance(repos_raw, list):
        return GitHubEvidence(username=username, status="error", error=f"unexpected status {status}")

    repos = [_stub_from_listing(r) for r in repos_raw[:max_repos]]
    wanted = _wanted(skills)

    # Forks are never skill evidence; rank the rest by listing relevance, then recency.
    candidates = [r for r in repos if not r.fork]
    candidates.sort(key=lambda r: r.pushed_at, reverse=True)
    candidates.sort(key=lambda r: _listing_relevance(r, wanted), reverse=True)

    def uncovered() -> list[str]:
        return [s for s in wanted
                if not any(repo_covers_skill(r, s) and repo_is_authored(r) for r in candidates)]

    analyzed = 0
    idx = 0
    covered_stop = False
    while idx < len(candidates):
        if client.blocked or analyzed >= max_analyzed:
            break
        if wanted and not uncovered():
            covered_stop = True
            break
        await _analyze_repo(client, username, candidates[idx], candidate_email_hints, max_commits)
        analyzed += 1
        idx += 1

    unreached = candidates[idx:]
    if client.blocked:
        for r in unreached:
            r.data_complete = False
            r.incomplete_reason = f"not examined: {client.blocked}"
    elif not covered_stop and unreached:
        missing = uncovered()
        for r in unreached:
            if _listing_relevance(r, missing):
                r.data_complete = False
                r.incomplete_reason = "not examined: per-candidate repository cap reached"

    ev = GitHubEvidence(username=username, status="ok", repos=repos,
                        requests_made=client.requests, rate_limit_remaining=client.remaining)
    bad = ev.incomplete_repos
    if bad:
        ev.status = "partial"
        ev.partial_reason = (
            f"{len(bad)} of {len(repos)} repositories could not be fully checked "
            f"({bad[0].incomplete_reason})"
        )
    return ev
