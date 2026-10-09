"""`bulk_import` flag (Spec 11 Stage 2 / Spec 17).

HONEST STATUS: the spec defines bulk_import as more than 80 percent of a repo's lines in one
commit. The collector does NOT measure lines: per-commit stats would cost one request per
commit and the rate-limit budget does not allow it, so it sets `lines_total = 1` and
`lines_by_candidate = 1` whenever the candidate has any commit (a placeholder). In practice the
flag therefore means: the repo has at most two commits and the candidate authored at least one
of them. These tests pin that documented behaviour; they do not claim real line-share detection.
"""
from app.modules.authenticity_engine.collectors.github import collect_github
from app.modules.authenticity_engine.matching import authenticity_flags
from tests.fixtures.github_fixtures import REPOS, make_fetch, make_world

REPO = REPOS[0]


def commit(login="priya"):
    return {"author": {"login": login}, "commit": {"author": {"email": f"{login}@example.com"}}}


async def collected(commits):
    world = make_world(
        repos=[REPO], languages={"ledger-service": {"Python": 100}},
        commits={"ledger-service": commits}, manifests={"ledger-service": set()},
        readme={"ledger-service": "A settlement ledger."}, extra_paths={"ledger-service": []},
    )
    gh = await collect_github("priya", fetch=make_fetch("priya", world=world))
    return gh, gh.repos[0]


async def test_single_dump_commit_is_flagged_bulk_import():
    gh, repo = await collected([commit()])
    assert repo.commits_total == 1 and repo.commits_by_candidate == 1
    assert "bulk_import" in repo.flags


async def test_two_commits_by_the_candidate_are_still_flagged():
    _, repo = await collected([commit(), commit()])
    assert "bulk_import" in repo.flags


async def test_three_or_more_commits_are_not_flagged():
    _, repo = await collected([commit(), commit(), commit()])
    assert "bulk_import" not in repo.flags


async def test_a_small_repo_the_candidate_did_not_author_is_not_flagged():
    _, repo = await collected([commit("someone-else")])
    assert repo.commits_by_candidate == 0 and repo.line_share == 0.0
    assert "bulk_import" not in repo.flags


async def test_line_share_is_a_placeholder_not_a_measurement():
    """Documents the limitation: line counts are never fetched, so two commits of any size
    look the same. If real per-commit line stats are added, this test should be replaced."""
    _, repo = await collected([commit(), commit()])
    assert repo.lines_total == 1 and repo.lines_by_candidate == 1


async def test_flag_reaches_the_report_with_a_neutral_explanation():
    gh, _ = await collected([commit()])
    flags = authenticity_flags(gh, [], ["ledger-service"])
    bulk = [f for f in flags if f["flag"] == "bulk_import"]
    assert bulk and bulk[0]["repo"] == "ledger-service"
    assert "single commit" in bulk[0]["detail"]
