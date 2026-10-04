"""Module B Stage 3: claim <-> collected-evidence matching (deterministic rubric).

The full spec calls for an LLM judge over retrieved evidence. This slice
implements the rubric deterministically against GitHub evidence only, which
keeps it testable and gives VERIFIED/CORROBORATED/UNSUPPORTED real meaning
without needing a live LLM call in the hot path. Every citation resolves to
a collected artifact by construction (post-check requirement: 0% hallucination).
"""
from __future__ import annotations

import re

from ...config import cfg
from .collectors.github import (
    CODE_EVIDENCE,
    GitHubEvidence,
    RepoEvidence,
    repo_covers_skill,
    skill_evidence_kind,
)
from .collectors.linkedin import LinkedInEvidence
from .collectors.portfolio import PortfolioEvidence

_repo_covers_skill = repo_covers_skill  # kept for judge.py, which imports the old name

STATUS_V = {
    "VERIFIED": 1.00, "CORROBORATED": 0.75, "WEAK": 0.40,
    "UNSUPPORTED": 0.00, "CONTRADICTED": -1.00, "UNVERIFIABLE": None,
}


def _partial_unverifiable(what: str, gh: GitHubEvidence) -> dict:
    """Our own rate limit or a transport error is not the candidate's problem: when a repo
    that could have answered was not fully checked, the honest answer is 'could not check'."""
    names = ", ".join(r.name for r in gh.incomplete_repos[:3])
    return {
        "status": "UNVERIFIABLE", "evidence": [],
        "rationale": f"GitHub could only be partly checked ({names or 'some repositories'} "
        f"not fully examined), so {what} could not be confirmed or ruled out.",
        "judge_confidence": 0.0,
    }


# ---------------------------------------------------------------------------------------
# Judge confidence (Spec 11 Stage 3 "judge confidence 0-1", Spec 16.2 ECE target).
#
# judge_confidence is the judge's belief that the STATUS it returned is the right one, and it
# is built only from properties of the collected evidence, never from the status alone:
#
#   VERIFIED skill   0.80 + 0.06*authorship + strength + 0.03*min(2, extra authored repos)
#   WEAK skill       0.55 + 0.10*(authorship / threshold) + 0.5*strength
#   UNSUPPORTED      0.70 + 0.20*n/(n+3), n = non-fork repos that were examined
#   VERIFIED project 0.60 + name_strength + 0.10*authorship + 0.05 if repo text corroborates
#   WEAK project     0.45 + name_strength/2 + 0.10*(authorship / threshold)
#   UNSUPPORTED proj 0.70 + 0.20*n/(n+3), n = non-fork repos listed
#   every GitHub-based result loses 0.10 when the collection was partial (A-18)
#
# strength is how the repo showed the skill: language bytes or manifest content 0.10 (machine
# derived), topic label 0.03 (a self-applied tag), README text only 0.0 (self-authored prose).
# Absence of evidence is capped well below presence: private work is invisible to us, so an
# UNSUPPORTED claim never exceeds 0.90 and never grows without bound as repos are added.
# The terms and their order come from reasoning about the evidence. The base levels were set
# once, after looking at TRAIN-split results only (never the test split), so that clean
# evidence sits near the accuracy a deterministic rule over collected data can reach.
# ---------------------------------------------------------------------------------------
_PARTIAL_PENALTY = 0.10
_PORTFOLIO_CONF = 0.30  # one self-authored page: the weakest evidence the rubric accepts


def _clip(x: float) -> float:
    # 0.95 ceiling: a deterministic matcher over third-party data is never certain.
    return round(max(0.0, min(0.95, x)), 3)


_KIND_STRENGTH = {"language": 0.10, "manifest": 0.10, "topic": 0.03, "readme": 0.0}


def _skill_strength(repo: RepoEvidence, skill: str) -> float:
    """How the repo showed the skill, from the collector's own token/alias matcher (no
    substring tests here: 'java' must never be found inside 'javascript')."""
    return _KIND_STRENGTH.get(skill_evidence_kind(repo, skill) or "", 0.0)


def _is_code_evidence(repo: RepoEvidence, skill: str) -> bool:
    return skill_evidence_kind(repo, skill) in CODE_EVIDENCE


def _is_authored(repo: RepoEvidence, min_auth: float) -> bool:
    return repo.authorship_ratio >= min_auth or (repo.owned and repo.commits_total == 0)


def _authorship_weight(repo: RepoEvidence) -> float:
    """0..1. An owned repo with no commit data (authorship unknowable) counts as half."""
    if repo.commits_total == 0:
        return 0.5 if repo.owned else 0.0
    return min(1.0, repo.authorship_ratio)


def _examined(gh: GitHubEvidence) -> int:
    return sum(1 for r in gh.repos if r.analyzed and not r.fork)


def _absence_conf(n: int, ceiling: float, gh: GitHubEvidence) -> float:
    c = 0.70 + ceiling * n / (n + 3)
    return _clip(c - (_PARTIAL_PENALTY if gh.status == "partial" else 0.0))


def judge_skill_claim(skill: str, gh: GitHubEvidence, required: bool) -> dict:
    """Returns {status, evidence[], rationale, judge_confidence}.

    UNSUPPORTED only when every relevant check completed; if any repo's data is incomplete
    and nothing positive was found, the result is UNVERIFIABLE (Spec 2.4)."""
    if not gh.has_data:
        return {
            "status": "UNVERIFIABLE", "evidence": [],
            "rationale": "No accessible GitHub source could confirm this claim.",
            "judge_confidence": 0.0,
        }
    min_auth = float(cfg("authenticity.github.authorship_ratio_min", 0.30))
    covering = [r for r in gh.repos if repo_covers_skill(r, skill)]
    # Spec 11 Stage 3: VERIFIED only from code/manifests. An authored repo that shows the
    # skill only through its README or a topic label is the candidate describing their
    # own work, which is corroboration (WEAK), not verification.
    authored = [r for r in covering if _is_authored(r, min_auth) and _is_code_evidence(r, skill)]
    penalty = _PARTIAL_PENALTY if gh.status == "partial" else 0.0
    evidence: list[dict] = []
    if authored:
        best = max(authored, key=lambda r: (_skill_strength(r, skill), _authorship_weight(r)))
        evidence.append({"source": "github", "citation": f"github.com/{gh.username}/{best.name}",
                         "note": f"authorship_ratio={best.authorship_ratio}"})
        conf = (0.80 + 0.06 * _authorship_weight(best) + _skill_strength(best, skill)
                + 0.03 * min(2, len(authored) - 1) - penalty)
        return {
            "status": "VERIFIED", "evidence": evidence,
            "rationale": f"Code and manifests in an owned, authored repository use {skill}.",
            "judge_confidence": _clip(conf),
        }
    for repo in covering:
        evidence.append({"source": "github", "citation": f"github.com/{gh.username}/{repo.name}",
                         "note": f"low authorship_ratio={repo.authorship_ratio}, evidence is weak"})
    if covering:
        best = max(covering, key=lambda r: (_authorship_weight(r), _skill_strength(r, skill)))
        conf = (0.55 + 0.10 * min(1.0, best.authorship_ratio / max(min_auth, 1e-9))
                + 0.5 * _skill_strength(best, skill) - penalty)
        return {
            "status": "WEAK", "evidence": evidence,
            "rationale": f"A repository mentions {skill} but authorship could not be confirmed.",
            "judge_confidence": _clip(conf),
        }
    if gh.incomplete_repos:
        return _partial_unverifiable(f"use of {skill}", gh)
    return {
        "status": "UNSUPPORTED", "evidence": [],
        "rationale": f"No collected repository shows evidence of {skill}.",
        "judge_confidence": _absence_conf(_examined(gh), 0.20, gh),
    }


# ----- project <-> repository correspondence ------------------------------------------------
# Words that describe the KIND of thing, not which thing it is. "Telemetry pipeline" and
# "ledger-pipeline" share only such a word, so they are different projects.
_GENERIC_WORDS = frozenset({
    "pipeline", "service", "app", "application", "api", "project", "tool", "system", "platform",
    "server", "client", "backend", "frontend", "web", "site", "website", "lib", "library",
    "engine", "dashboard", "bot", "util", "kit", "toolkit", "manager", "demo", "repo", "code",
    "core", "my", "the", "a", "an", "and", "of", "for", "with", "in", "on", "to", "old", "new",
})
# Technology names. A repository named only after a technology (a fork of mysql, docker) is
# not a project the candidate named; a skill word never makes a project correspondence.
_TECH_WORDS = frozenset({
    "python", "java", "javascript", "typescript", "js", "ts", "node", "nodejs", "react", "vue",
    "angular", "django", "flask", "fastapi", "docker", "kubernetes", "k8s", "mysql", "postgres",
    "postgresql", "mongodb", "mongo", "redis", "kafka", "aws", "azure", "gcp", "go", "golang",
    "rust", "ruby", "rails", "php", "cpp", "csharp", "dotnet", "sql", "nosql", "terraform",
    "ansible", "graphql", "git", "github", "linux", "html", "css", "sass", "spring", "swift",
    "kotlin", "android", "ios", "tensorflow", "pytorch", "pandas", "numpy", "spark", "hadoop",
    "elasticsearch", "nginx", "jenkins", "sqlite", "oracle", "bash", "rabbitmq", "cassandra",
})
_NAME_SIMILARITY = 0.85
_COVERAGE_MIN = 2 / 3


def _norm_token(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _words(text: str) -> list[str]:
    return [_norm_token(w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def _correspondence(repo_name: str, claim_text: str, project_name: str | None = None,
                    ignore: frozenset[str] = frozenset()) -> float:
    """0.0 when the repository does not correspond to the project, else a strength in (0, 1].

    Two routes, both strict:
      name route   the normalised repo name is at least 85% similar to the project NAME
                   (the given name, or the claim text when it is itself short enough to be a name)
      token route  at least 2/3 of the repo name's meaningful tokens (generic words and pure
                   technology names removed) appear in the claim text, and at least one matched
                   token is not a technology name. A repo whose every token is generic or a
                   technology has no meaningful tokens and can only match by the name route.
    `ignore` holds further words that never count, such as the resume's SKILL claim words."""
    import difflib

    repo_words = _words(repo_name)
    if not repo_words:
        return 0.0
    name = project_name if project_name else (claim_text if len(_words(claim_text)) <= 5 else "")
    if name:
        nw = _words(name)
        if nw and difflib.SequenceMatcher(None, " ".join(repo_words), " ".join(nw)).ratio() >= _NAME_SIMILARITY:
            return 1.0
    skip = _GENERIC_WORDS | ignore
    meaningful = [w for w in repo_words if w not in skip]
    non_tech = [w for w in meaningful if w not in _TECH_WORDS]
    if not non_tech:
        return 0.0
    have = set(_words(claim_text)) | set(_words(project_name or ""))
    hit = [w for w in meaningful if w in have]
    if len(hit) / len(meaningful) < _COVERAGE_MIN or not any(w not in _TECH_WORDS for w in hit):
        return 0.0
    return len(hit) / len(meaningful)


def project_matches_repo(repo: RepoEvidence, claim_text: str, project_name: str | None = None,
                         ignore: frozenset[str] = frozenset()) -> bool:
    """Public so judge.py grounds an LLM citation with exactly the same rule."""
    return _correspondence(repo.name, claim_text, project_name, ignore) > 0.0


def judge_project_claim(project_text: str, gh: GitHubEvidence, project_name: str | None = None) -> dict:
    if not gh.has_data:
        return {
            "status": "UNVERIFIABLE", "evidence": [],
            "rationale": "No accessible GitHub source could confirm this project.",
            "judge_confidence": 0.0,
        }
    min_auth = float(cfg("authenticity.github.authorship_ratio_min", 0.30))
    scored = [(_correspondence(r.name, project_text, project_name), r) for r in gh.repos]
    matches = [(s, r) for s, r in scored if s > 0.0]
    penalty = _PARTIAL_PENALTY if gh.status == "partial" else 0.0
    if matches:
        # An authored, non-fork repository always beats a fork, however well the fork's name
        # fits: the fork is somebody else's code, the authored repo is the candidate's.
        def rank(m: tuple[float, RepoEvidence]) -> tuple:
            s, r = m
            return (_is_authored(r, min_auth) and not r.fork, not r.fork, s, _authorship_weight(r))

        strength, repo = max(matches, key=rank)
        status = "VERIFIED" if _is_authored(repo, min_auth) and not repo.fork else "WEAK"
        name_strength = 0.15 if strength >= 1.0 else 0.10 * strength
        if status == "VERIFIED":
            blob = f"{repo.description} {repo.readme_excerpt}".lower()
            claim_words = {w for w in _words(project_text) if w not in _GENERIC_WORDS and len(w) > 2}
            corroborated = len(claim_words & set(_words(blob))) >= 3
            conf = (0.60 + name_strength + 0.10 * _authorship_weight(repo)
                    + (0.05 if corroborated else 0.0) - penalty)
        else:
            conf = (0.45 + name_strength / 2
                    + 0.10 * min(1.0, repo.authorship_ratio / max(min_auth, 1e-9)) - penalty)
        return {
            "status": status,
            "evidence": [{"source": "github", "citation": f"github.com/{gh.username}/{repo.name}",
                         "note": f"authorship_ratio={repo.authorship_ratio}"}],
            "rationale": f"A repository named '{repo.name}' matches this project claim.",
            "judge_confidence": _clip(conf),
        }
    if gh.incomplete_repos:
        return _partial_unverifiable("this project", gh)
    return {
        "status": "UNSUPPORTED", "evidence": [],
        "rationale": "No repository name or description matches this project.",
        "judge_confidence": _absence_conf(sum(1 for r in gh.repos if not r.fork), 0.20, gh),
    }


def authenticity_flags(gh: GitHubEvidence, claims: list[dict] | None = None,
                       project_names: list[str] | None = None) -> list[dict]:
    """Repo flags only for repos the resume actually relies on: cited as evidence for a
    claim, or corresponding to a PROJECT claim under the strict rule of `_correspondence`.
    Forking a public repo and never touching it is normal, so a fork the candidate never
    presents as a project is not reported as 'claimed as own'; a SKILL claim that merely
    shares a word with a repo name (a fork called 'mysql') never counts (A-19).
    claims=None (direct unit use) reports every flagged repo."""
    relied_on = None
    if claims is not None:
        relied_on = set()
        for c in claims:
            for e in c.get("evidence", []):
                relied_on.add(str(e.get("citation", "")).rsplit("/", 1)[-1])
        skill_words = frozenset(w for c in claims if c.get("type") == "SKILL"
                                for w in _words(c["text"]))
        project_texts = [c["text"] for c in claims if c.get("type") == "PROJECT"]
        names = [n for n in project_names or [] if n]
        for repo in gh.repos:
            if any(_correspondence(repo.name, t, None, skill_words) for t in project_texts) or any(
                _correspondence(repo.name, n, n, skill_words) for n in names
            ):
                relied_on.add(repo.name)
    out = []
    for repo in gh.repos:
        if relied_on is not None and repo.name not in relied_on:
            continue
        for flag in repo.flags:
            out.append({"flag": flag, "repo": repo.name, "detail": _flag_detail(flag, repo)})
    return out


def _flag_detail(flag: str, repo: RepoEvidence) -> str:
    return {
        "bulk_import": f"Most of the code in {repo.name} arrived in a single commit.",
        "fork_claimed_as_own": f"{repo.name} is a fork with no commits from the candidate.",
        "tutorial_clone": f"{repo.name}'s README references tutorial or coursework material.",
    }.get(flag, flag)


def judge_role_claim(role_title: str, li: LinkedInEvidence, company: str | None = None) -> dict:
    """Resume vs LinkedIn are both self-reported, so this can only ever reach
    CORROBORATED (Spec 11 Stage 3 rule), never VERIFIED.

    Pairs by employer first when `company` is known - matching by title alone
    can corroborate a role against the wrong employer's listing on LinkedIn
    (the same class of bug fixed in consistency.check_linkedin_consistency;
    see ASSUMPTIONS.md). Without a company (older claim sites, or a resume
    role with no parsed company) this falls back to title-only matching."""
    if li.status != "ok" or not li.roles:
        return {
            "status": "UNVERIFIABLE", "evidence": [],
            "rationale": "No accessible LinkedIn export could confirm this role.",
            "judge_confidence": 0.0,
        }
    import difflib

    candidates = li.roles
    if company:
        by_company = [r for r in li.roles
                      if difflib.SequenceMatcher(None, company.lower(), r.company.lower()).ratio() >= 0.6]
        if by_company:
            candidates = by_company
        else:
            return {
                "status": "UNSUPPORTED", "evidence": [],
                "rationale": f"No role at '{company}' was found in the LinkedIn export.",
                "judge_confidence": 0.5,
            }

    best = max(candidates, key=lambda r: difflib.SequenceMatcher(None, role_title.lower(), r.title.lower()).ratio())
    ratio = difflib.SequenceMatcher(None, role_title.lower(), best.title.lower()).ratio()
    if ratio >= 0.6:
        return {
            "status": "CORROBORATED",
            "evidence": [{"source": "linkedin", "citation": f"linkedin_export:role:{best.title}",
                         "note": f"title similarity {ratio:.2f}"}],
            "rationale": "A matching role appears in the candidate's LinkedIn export.",
            # 0.55 + 0.30*title similarity, +0.10 when the employer was paired too: two
            # independent attributes agreeing, but both self-reported, so it is capped at the 0.95 ceiling.
            "judge_confidence": _clip(0.55 + 0.30 * ratio + (0.10 if company else 0.0)),
        }
    return {
        "status": "UNSUPPORTED", "evidence": [],
        "rationale": "No matching role was found in the LinkedIn export.",
        "judge_confidence": 0.5,
    }


def judge_skill_claim_with_portfolio(skill: str, gh: GitHubEvidence, pf: PortfolioEvidence,
                                     required: bool) -> dict:
    """Combines GitHub (can VERIFY) with a portfolio (can only WEAK-corroborate,
    since a portfolio page is self-authored - same rule as resume vs LinkedIn)."""
    gh_judged = judge_skill_claim(skill, gh, required)
    if gh_judged["status"] == "VERIFIED":
        return gh_judged
    if pf.status == "ok" and skill.lower() in {t.lower() for t in pf.tech_mentions}:
        if gh_judged["status"] == "UNVERIFIABLE":
            return {
                "status": "WEAK",
                "evidence": [{"source": "portfolio", "citation": pf.url,
                             "note": f"{skill} appears on the candidate's portfolio page"}],
                "rationale": f"The candidate's own portfolio mentions {skill}, but a "
                "self-authored page is weaker evidence than code.",
                "judge_confidence": _PORTFOLIO_CONF,
            }
    return gh_judged


def portfolio_flags(pf: PortfolioEvidence) -> list[dict]:
    """Dead demo links are WEAK evidence only, never a contradiction (Spec 11 Stage 2)."""
    return [
        {"flag": "dead_demo_link", "repo": pf.url,
         "detail": f"The live demo link {c['url']} did not return a successful response."}
        for c in pf.live_demo_checks if not c["ok"]
    ]
