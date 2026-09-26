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
from .collectors.github import GitHubEvidence, RepoEvidence
from .collectors.linkedin import LinkedInEvidence
from .collectors.portfolio import PortfolioEvidence

STATUS_V = {
    "VERIFIED": 1.00, "CORROBORATED": 0.75, "WEAK": 0.40,
    "UNSUPPORTED": 0.00, "CONTRADICTED": -1.00, "UNVERIFIABLE": None,
}


def _repo_covers_skill(repo: RepoEvidence, skill: str) -> bool:
    s = skill.lower()
    if any(s in lang.lower() for lang in repo.languages):
        return True
    if any(s in t.lower() for t in repo.topics):
        return True
    manifest_hits = {
        "requirements.txt": {"python", "fastapi", "django", "flask"},
        "pyproject.toml": {"python", "fastapi", "django", "flask"},
        "package.json": {"javascript", "typescript", "react", "vue", "node"},
        "pom.xml": {"java"},
        "go.mod": {"go", "golang"},
        "Dockerfile": {"docker"},
    }
    for m in repo.manifests_found:
        if s in manifest_hits.get(m, set()):
            return True
    return s in repo.readme_excerpt.lower() if repo.readme_excerpt else False


def judge_skill_claim(skill: str, gh: GitHubEvidence, required: bool) -> dict:
    """Returns {status, evidence[], rationale, judge_confidence}."""
    if gh.status != "ok":
        return {
            "status": "UNVERIFIABLE", "evidence": [],
            "rationale": "No accessible GitHub source could confirm this claim.",
            "judge_confidence": 0.0,
        }
    min_auth = float(cfg("authenticity.github.authorship_ratio_min", 0.30))
    evidence = []
    best_status = "UNSUPPORTED"
    for repo in gh.repos:
        if not _repo_covers_skill(repo, skill):
            continue
        if repo.authorship_ratio >= min_auth or (repo.owned and repo.commits_total == 0):
            evidence.append(
                {"source": "github", "citation": f"github.com/{gh.username}/{repo.name}",
                 "note": f"authorship_ratio={repo.authorship_ratio}"}
            )
            best_status = "VERIFIED"
            break
        else:
            evidence.append(
                {"source": "github", "citation": f"github.com/{gh.username}/{repo.name}",
                 "note": f"low authorship_ratio={repo.authorship_ratio}, evidence is weak"}
            )
            best_status = "WEAK" if best_status == "UNSUPPORTED" else best_status
    rationale = {
        "VERIFIED": f"Code and manifests in an owned, authored repository use {skill}.",
        "WEAK": f"A repository mentions {skill} but authorship could not be confirmed.",
        "UNSUPPORTED": f"No collected repository shows evidence of {skill}.",
    }[best_status]
    return {
        "status": best_status, "evidence": evidence, "rationale": rationale,
        "judge_confidence": 0.85 if best_status == "VERIFIED" else 0.5 if best_status == "WEAK" else 0.6,
    }


def judge_project_claim(project_text: str, gh: GitHubEvidence) -> dict:
    if gh.status != "ok":
        return {
            "status": "UNVERIFIABLE", "evidence": [],
            "rationale": "No accessible GitHub source could confirm this project.",
            "judge_confidence": 0.0,
        }
    tokens = set(re.findall(r"[a-z0-9]+", project_text.lower()))
    for repo in gh.repos:
        name_tokens = set(re.findall(r"[a-z0-9]+", repo.name.lower()))
        if name_tokens & tokens and len(name_tokens & tokens) >= 1:
            min_auth = float(cfg("authenticity.github.authorship_ratio_min", 0.30))
            status = "VERIFIED" if repo.authorship_ratio >= min_auth else "WEAK"
            return {
                "status": status,
                "evidence": [{"source": "github", "citation": f"github.com/{gh.username}/{repo.name}",
                             "note": f"authorship_ratio={repo.authorship_ratio}"}],
                "rationale": f"A repository named '{repo.name}' matches this project claim.",
                "judge_confidence": 0.8 if status == "VERIFIED" else 0.45,
            }
    return {
        "status": "UNSUPPORTED", "evidence": [],
        "rationale": "No repository name or description matches this project.",
        "judge_confidence": 0.55,
    }


def authenticity_flags(gh: GitHubEvidence) -> list[dict]:
    out = []
    for repo in gh.repos:
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
            "judge_confidence": 0.7,
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
                "judge_confidence": 0.35,
            }
    return gh_judged


def portfolio_flags(pf: PortfolioEvidence) -> list[dict]:
    """Dead demo links are WEAK evidence only, never a contradiction (Spec 11 Stage 2)."""
    return [
        {"flag": "dead_demo_link", "repo": pf.url,
         "detail": f"The live demo link {c['url']} did not return a successful response."}
        for c in pf.live_demo_checks if not c["ok"]
    ]
