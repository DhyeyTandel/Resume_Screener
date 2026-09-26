"""Module B Stage 2/3: portfolio collector and dead-link/robots.txt handling."""
from app.modules.authenticity_engine.collectors.github import GitHubEvidence
from app.modules.authenticity_engine.collectors.portfolio import PortfolioEvidence, collect_portfolio
from app.modules.authenticity_engine.matching import judge_skill_claim_with_portfolio, portfolio_flags
from tests.fixtures.portfolio_fixtures import (ROBOTS_DISALLOW_ALL, ROBOTS_DISALLOW_PRIVATE,
                                               make_fetch)


async def test_collector_extracts_projects_and_tech_mentions():
    pf = await collect_portfolio("https://priya.dev", fetch=make_fetch())
    assert pf.status == "ok"
    assert "Ledger Service" in pf.projects
    assert "fastapi" in pf.tech_mentions
    assert "postgresql" in pf.tech_mentions


async def test_live_demo_checked_and_dead_link_flagged_not_a_contradiction():
    pf = await collect_portfolio("https://priya.dev", fetch=make_fetch())
    dead = [c for c in pf.live_demo_checks if "dead-demo" in c["url"]]
    assert dead and dead[0]["ok"] is False
    flags = portfolio_flags(pf)
    assert any(f["flag"] == "dead_demo_link" for f in flags)
    # Crucially: dead_demo_link is a "flag", never wired into `contradictions`.
    assert all(f["flag"] != "contradiction" for f in flags)


async def test_robots_disallow_all_blocks_collection():
    pf = await collect_portfolio("https://priya.dev", fetch=make_fetch(robots=ROBOTS_DISALLOW_ALL))
    assert pf.status == "disallowed"


async def test_robots_disallow_specific_path_allows_root():
    pf = await collect_portfolio("https://priya.dev", fetch=make_fetch(robots=ROBOTS_DISALLOW_PRIVATE))
    assert pf.status == "ok"


async def test_missing_url_never_hits_network():
    pf = await collect_portfolio(None)
    assert pf.status == "missing"


async def test_404_degrades_to_error_not_a_crash():
    pf = await collect_portfolio("https://priya.dev", fetch=make_fetch(home_status=404))
    assert pf.status == "error"


async def test_skill_verified_by_github_ignores_weaker_portfolio_evidence():
    from tests.fixtures.github_fixtures import make_fetch as gh_fetch
    from app.modules.authenticity_engine.collectors.github import collect_github

    gh = await collect_github("priya", fetch=gh_fetch("priya"))
    pf = await collect_portfolio("https://priya.dev", fetch=make_fetch())
    j = judge_skill_claim_with_portfolio("Python", gh, pf, required=True)
    assert j["status"] == "VERIFIED"  # GitHub evidence wins, unchanged by portfolio


async def test_skill_only_on_portfolio_is_weak_not_verified():
    pf = await collect_portfolio("https://priya.dev", fetch=make_fetch())
    j = judge_skill_claim_with_portfolio(
        "Kafka", GitHubEvidence(username="", status="missing"), pf, required=True)
    assert j["status"] == "WEAK"


async def test_skill_nowhere_is_still_unverifiable():
    j = judge_skill_claim_with_portfolio(
        "Rust", GitHubEvidence(username="", status="missing"),
        PortfolioEvidence(url="", status="missing"), required=True)
    assert j["status"] == "UNVERIFIABLE"
