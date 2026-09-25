"""Module B Stage 2/3: GitHub collector and claim-evidence matching."""
from app.modules.authenticity_engine.collectors.github import collect_github
from app.modules.authenticity_engine.matching import (authenticity_flags, judge_project_claim,
                                                       judge_skill_claim)
from tests.fixtures.github_fixtures import make_fetch


async def test_collector_computes_authorship_ratio():
    gh = await collect_github("priya", fetch=make_fetch("priya"))
    assert gh.status == "ok"
    ledger = next(r for r in gh.repos if r.name == "ledger-service")
    assert ledger.authorship_ratio == 0.9  # 18 of 20 commits
    assert ledger.manifests_found == ["requirements.txt", "Dockerfile"]


async def test_collector_flags_fork_claimed_as_own_and_tutorial_clone():
    gh = await collect_github("priya", fetch=make_fetch("priya"))
    clone = next(r for r in gh.repos if r.name == "old-tutorial-clone")
    assert "fork_claimed_as_own" in clone.flags
    assert "tutorial_clone" in clone.flags


async def test_collector_missing_username_never_hits_network():
    gh = await collect_github(None)
    assert gh.status == "missing"


async def test_collector_404_and_403_degrade_without_raising():
    gh_404 = await collect_github("ghost", fetch=make_fetch(not_found=True))
    assert gh_404.status == "error" and "not found" in gh_404.error
    gh_403 = await collect_github("priya", fetch=make_fetch(rate_limited=True))
    assert gh_403.status == "error" and "rate limit" in gh_403.error


async def test_skill_verified_from_authored_repo():
    gh = await collect_github("priya", fetch=make_fetch("priya"))
    j = judge_skill_claim("Python", gh, required=True)
    assert j["status"] == "VERIFIED"
    assert j["evidence"][0]["citation"].startswith("github.com/priya/")


async def test_skill_unverifiable_when_no_github_collected():
    from app.modules.authenticity_engine.collectors.github import GitHubEvidence

    j = judge_skill_claim("Python", GitHubEvidence(username="", status="missing"), required=True)
    assert j["status"] == "UNVERIFIABLE"


async def test_project_matched_by_repo_name():
    gh = await collect_github("priya", fetch=make_fetch("priya"))
    j = judge_project_claim("Ledger Service: FastAPI and PostgreSQL settlement service", gh)
    assert j["status"] == "VERIFIED"


async def test_citations_always_resolve_to_a_collected_repo():
    """0% hallucinated evidence: every citation names a repo the collector actually saw."""
    gh = await collect_github("priya", fetch=make_fetch("priya"))
    known = {f"github.com/priya/{r.name}" for r in gh.repos}
    for skill in ("Python", "FastAPI", "Kafka"):
        j = judge_skill_claim(skill, gh, required=True)
        for e in j["evidence"]:
            assert e["citation"] in known


async def test_authenticity_flags_cite_the_repo():
    gh = await collect_github("priya", fetch=make_fetch("priya"))
    flags = authenticity_flags(gh)
    assert any(f["flag"] == "fork_claimed_as_own" and f["repo"] == "old-tutorial-clone" for f in flags)
