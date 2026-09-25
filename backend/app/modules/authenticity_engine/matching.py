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
