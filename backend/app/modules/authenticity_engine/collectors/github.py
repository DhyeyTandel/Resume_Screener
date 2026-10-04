"""GitHub evidence collector (Module B Stage 2, public REST, optional token).

Testability: `fetch` is injectable so tests can replay recorded fixtures
instead of hitting the network (Spec: no live network calls in CI).
"""
from __future__ import annotations

import base64
import json
import re
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache

from ....config import cfg
from ....db.cache import cache_enabled, cached_fetch
from ...skill_intelligence.transfer import canonical, graph

# A fetch returns (status, body) or (status, body, response_headers). Headers are optional so
# older injected fetches keep working; when present, X-RateLimit-Remaining is honoured.
Fetch = Callable[[str, dict | None], Awaitable[tuple]]

MANIFESTS = (
    "requirements.txt", "pyproject.toml", "package.json", "pom.xml", "go.mod", "Dockerfile",
)

# Tutorial-clone detection (Spec 11 Stage 2 means "a clone of a tutorial", not "a README that
# mentions a tutorial"). A README is flagged only when it names where the work came from
# (PROVENANCE: a course, a platform, "following along") AND a second, distinct marker agrees.
# The bare word "tutorial" is a WEAK marker: on its own it never flags, because genuine
# projects say "see the tutorial section" or "tutorial" in docs all the time.
_PROVENANCE = re.compile(
    r"\b(freecodecamp|udemy|coursera|bootcamp[- ]?project|clone[- ]?coding|following along)\b", re.I)
_ASSIGNMENT = re.compile(r"\b(exercise \d+|starter code)\b", re.I)
_WEAK = re.compile(r"\btutorials?\b", re.I)
# A fork is listing-only evidence, so any marker in its name or description is enough.
TUTORIAL_MARKERS = re.compile(
    r"\b(tutorial|freecodecamp|udemy|coursera|bootcamp[- ]?project|clone[- ]?coding|"
    r"following along|exercise \d+|starter code)\b", re.I,
)


def is_tutorial_clone(text: str) -> bool:
    """Needs a provenance marker plus at least one other distinct marker (see above)."""
    found = {m.group(0).lower().split()[0] for rx in (_PROVENANCE, _ASSIGNMENT, _WEAK)
             for m in rx.finditer(text)}
    return bool(_PROVENANCE.search(text)) and len(found) >= 2


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
    # Dependency names read from manifest CONTENT (lowercase), only when a framework claim
    # needed it. Empty means "not read", never "has no dependencies".
    dependencies: list[str] = field(default_factory=list)
    manifests_read: list[str] = field(default_factory=list)

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


def _norm(s: str) -> str:
    """'Vue.js', 'vue-js' and 'vuejs' compare equal; 'java' and 'javascript' do not."""
    return re.sub(r"[^a-z0-9+#]+", "", s.lower())


# A manifest FILE proves only the language ecosystem (Spec 11 Stage 3: skills come from code or
# manifests, and a file name is not a dependency). Frameworks need the manifest CONTENT.
_LANGUAGE_MANIFESTS = {
    "requirements.txt": {"python"}, "pyproject.toml": {"python"},
    "package.json": {"javascript", "node", "nodejs"}, "pom.xml": {"java"},
    "go.mod": {"go", "golang"}, "Dockerfile": {"docker"},
}
# canonical skill -> (manifest files that can declare it, dependency names that prove it)
_PY = ("requirements.txt", "pyproject.toml")
_FRAMEWORK_DEPS: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    "fastapi": (_PY, frozenset({"fastapi"})),
    "django": (_PY, frozenset({"django", "djangorestframework"})),
    "flask": (_PY, frozenset({"flask"})),
    "pytest": (_PY, frozenset({"pytest"})),
    "kafka": (_PY + ("package.json",), frozenset({"kafka-python", "confluent-kafka", "aiokafka", "kafkajs"})),
    "rabbitmq": (_PY + ("package.json",), frozenset({"pika", "aio-pika", "amqplib"})),
    "mongodb": (_PY + ("package.json",), frozenset({"pymongo", "motor", "mongoose", "mongodb"})),
    "postgresql": (_PY + ("package.json",), frozenset({"psycopg2", "psycopg2-binary", "psycopg", "asyncpg", "pg"})),
    "react": (("package.json",), frozenset({"react", "react-dom"})),
    "vue": (("package.json",), frozenset({"vue"})),
    "angular": (("package.json",), frozenset({"@angular/core"})),
    "typescript": (("package.json",), frozenset({"typescript"})),
}
# Aliases that are generic English or too short to trust inside free text (README prose).
# They still match a language name or a topic exactly.
_FREE_TEXT_SKIP = {"rest", "restful apis", "api design", "testing", "unit testing", "containers",
                   "containerization", "ci", "cd", "go", "py", "es6"}


@lru_cache(maxsize=1024)
def _skill_terms(skill: str) -> tuple[str, frozenset[str]]:
    """(canonical name, every spelling) from the skill graph, so 'Vue.js', 'K8s' and 'Apache
    Kafka' resolve like 'Vue', 'Kubernetes' and 'Kafka'. Unknown skills stand for themselves."""
    s = skill.strip().lower()
    canon = canonical(s) or s
    node = graph()["skills"].get(canon, {})
    return canon, frozenset({canon, s, *node.get("aliases", [])})


@lru_cache(maxsize=1024)
def _free_text_rx(skill: str) -> re.Pattern | None:
    terms = [t for t in _skill_terms(skill)[1] if t not in _FREE_TEXT_SKIP and len(t) >= 2]
    if not terms:
        return None
    alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")


def _mentions(text: str, skill: str) -> bool:
    """Whole-token mention: 'Java' is not in 'JavaScript', 'go' is not in 'Django'."""
    rx = _free_text_rx(skill)
    return bool(rx and text and rx.search(text.lower()))


def _framework_manifests(skill: str) -> tuple[str, ...]:
    return _FRAMEWORK_DEPS.get(_skill_terms(skill)[0], ((), frozenset()))[0]


def _covered_without_content(repo: RepoEvidence, skill: str) -> bool:
    """Evidence from the listing, language bytes, topics, manifest file names (language level
    only) and README; everything except manifest content."""
    canon, terms = _skill_terms(skill)
    normed = {_norm(t) for t in terms}
    for lang in repo.languages:
        n = _norm(lang)
        if {"dockerfile": "docker"}.get(n, n) in normed:
            return True
    if any(_norm(t) in normed for t in repo.topics):
        return True
    if any(normed & _LANGUAGE_MANIFESTS.get(m, set()) for m in repo.manifests_found):
        return True
    return _mentions(repo.readme_excerpt, skill)


CODE_EVIDENCE = frozenset({"language", "manifest"})


def skill_evidence_kind(repo: RepoEvidence, skill: str) -> str | None:
    """The strongest way an analyzed repo shows a skill: 'language' (language bytes),
    'manifest' (manifest file proving the language, or a real dependency), 'topic'
    (a label the owner set) or 'readme' (prose the owner wrote); None if not shown.
    Spec 11 Stage 3: VERIFIED only from code/manifests, so only the first two count as
    code evidence. Topics and README are the candidate describing their own work."""
    if not repo.analyzed:
        return None
    canon, terms = _skill_terms(skill)
    normed = {_norm(t) for t in terms}
    if any({"dockerfile": "docker"}.get(_norm(lang), _norm(lang)) in normed for lang in repo.languages):
        return "language"
    if any(normed & _LANGUAGE_MANIFESTS.get(m, set()) for m in repo.manifests_found):
        return "manifest"
    deps = _FRAMEWORK_DEPS.get(canon)
    if deps and set(repo.dependencies) & deps[1]:
        return "manifest"
    if canon == "ci/cd" and repo.has_ci:  # a .github/workflows file is pipeline code
        return "manifest"
    if any(_norm(t) in normed for t in repo.topics):
        return "topic"
    if _mentions(repo.readme_excerpt, skill):
        return "readme"
    return None


def repo_covers_skill(repo: RepoEvidence, skill: str) -> bool:
    """True when the collected data of an analyzed repo shows the skill. Forks and repos that
    were skipped are never skill evidence (they have no deep data). Matching is by whole
    token or graph alias, never by substring; a manifest file name proves only its language,
    and a framework needs a real dependency in the manifest content."""
    if not repo.analyzed:
        return False
    if _covered_without_content(repo, skill):
        return True
    deps = _FRAMEWORK_DEPS.get(_skill_terms(skill)[0])
    return bool(deps and set(repo.dependencies) & deps[1])


def parse_dependencies(manifest: str, text: str) -> set[str]:
    """Lowercase dependency names declared in a manifest's content. Unparseable means empty."""
    names: set[str] = set()

    def req(spec: str) -> None:
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)", spec)
        if m:
            names.add(m.group(1).lower().replace("_", "-"))

    try:
        if manifest == "requirements.txt":
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-"):
                    req(line)
        elif manifest == "pyproject.toml":
            data = tomllib.loads(text)
            proj = data.get("project", {})
            for spec in proj.get("dependencies", []):
                req(str(spec))
            for group in (proj.get("optional-dependencies") or {}).values():
                for spec in group:
                    req(str(spec))
            poetry = (data.get("tool") or {}).get("poetry", {})
            tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
            tables += [g.get("dependencies", {}) for g in (poetry.get("group") or {}).values()]
            for t in tables:
                names.update(str(k).lower().replace("_", "-") for k in t)
        elif manifest == "package.json":
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                names.update(str(k).lower() for k in (data.get(key) or {}))
    except (ValueError, AttributeError, TypeError):
        return set()
    return names


def repo_is_authored(repo: RepoEvidence) -> bool:
    min_auth = float(cfg("authenticity.github.authorship_ratio_min", 0.30))
    return repo.authorship_ratio >= min_auth or (repo.owned and repo.commits_total == 0)


def _listing_relevance(repo: RepoEvidence, skills: list[str]) -> int:
    """How many of the wanted skills the *listing alone* (language, topics, name,
    description) suggests this repo could answer. Used only to order and to size the
    incompleteness of repos we did not open."""
    hay = " ".join([repo.primary_language, repo.name, repo.description, *repo.topics]).lower()
    def hit(skill: str) -> bool:
        normed = {_norm(t) for t in _skill_terms(skill)[1]}
        exact = {_norm(repo.primary_language), *(_norm(t) for t in repo.topics)}
        return _mentions(hay, skill) or bool(normed & exact)

    return sum(1 for s in skills if hit(s))


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
        # Manifest CONTENT reads (framework claims only) are budgeted separately, so they can
        # never crowd out the per-repo checks that the base budget was sized for (A-18).
        self.manifest_reads = 0
        self.max_manifest_reads = int(cfg("authenticity.github.max_manifest_reads", 6))

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
    client: _Client, username: str, repo: RepoEvidence, hints: list[str] | None, max_commits: int,
    need: list[str] | None = None,
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
    if is_tutorial_clone(f"{repo.name} {repo.description} {repo.readme_excerpt}"):
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

    # Framework claims still unanswered need the manifest CONTENT (a file name proves only the
    # language). One request per manifest that could answer them, at most two per repo, only
    # when nothing else in this repo already covers the skill, and only up to a per-candidate cap.
    pending = [s for s in (need or []) if not _covered_without_content(repo, s)]
    for manifest in MANIFESTS:
        if not pending or client.blocked or manifest not in repo.manifests_found:
            continue
        if not any(manifest in _framework_manifests(s) for s in pending):
            continue
        if client.manifest_reads >= client.max_manifest_reads or len(repo.manifests_read) >= 2:
            break
        client.manifest_reads += 1
        repo.manifests_read.append(manifest)
        got = await call(f"{base}/contents/{manifest}")
        if got and got[0] and isinstance(got[1], dict) and got[1].get("content"):
            try:
                text = base64.b64decode(got[1]["content"]).decode("utf-8", "replace")
            except Exception:
                continue
            repo.dependencies = sorted({*repo.dependencies, *parse_dependencies(manifest, text)})
            pending = [s for s in pending if not repo_covers_skill(repo, s)]

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
    if not repos:
        # Nothing was checkable, which is different from "checked and found nothing" (Spec 2.4).
        return GitHubEvidence(username=username, status="missing", error="no public repositories",
                              requests_made=client.requests, rate_limit_remaining=client.remaining)
    if all(r.fork for r in repos):
        # Forks are never skill evidence and are not opened, so there is no authored code to
        # check: the same "nothing checkable" case. The stubs are kept so that the fork flags
        # (fork_claimed_as_own, tutorial_clone) still reach authenticity_flags().
        return GitHubEvidence(
            username=username, status="missing", repos=repos,
            error=f"no authored repositories (all {len(repos)} public repositories are forks)",
            requests_made=client.requests, rate_limit_remaining=client.remaining)
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
        await _analyze_repo(client, username, candidates[idx], candidate_email_hints, max_commits,
                            need=uncovered() if wanted else None)
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
