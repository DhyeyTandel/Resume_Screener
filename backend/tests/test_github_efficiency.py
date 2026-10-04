"""GitHub collector efficiency, caching and fairness under rate limits (Spec 2.4, 6.4, 11 Stage 2).

No live network: every test uses an injected fetch or a recorded fixture."""
import sqlite3

from app.db.cache import Cache, cached_fetch
from app.modules.authenticity_engine.collectors.github import collect_github
from app.modules.authenticity_engine.engine import _recruiter_summary
from app.modules.authenticity_engine.matching import judge_project_claim, judge_skill_claim
from tests.fixtures.github_fixtures import make_fetch, make_fetch_with_headers, make_world


def _three_repo_world():
    """alpha: Python, beta: Go (the repo that gets rate limited), gamma: Java."""
    repos = [
        {"name": n, "fork": False, "created_at": "2022-01-01T00:00:00Z", "pushed_at": p,
         "topics": [], "default_branch": "main", "language": lang, "description": ""}
        for n, p, lang in [("alpha", "2024-03-01T00:00:00Z", "Python"),
                           ("beta", "2024-02-01T00:00:00Z", "Go"),
                           ("gamma", "2024-01-01T00:00:00Z", "Java")]
    ]
    me = {"author": {"login": "priya"}, "commit": {"author": {"email": ""}}}
    return make_world(
        repos,
        languages={"alpha": {"Python": 100}, "beta": {"Go": 100}, "gamma": {"Java": 100}},
        commits={"alpha": [me] * 5, "beta": [me] * 5, "gamma": [me] * 5},
        manifests={"alpha": {"requirements.txt"}, "beta": {"go.mod"}, "gamma": {"pom.xml"}},
        readme={"alpha": "alpha", "beta": "beta", "gamma": "gamma"},
        extra_paths={},
    )


async def test_request_count_drops_for_two_repo_fixture():
    fetch = make_fetch("priya")
    gh = await collect_github("priya", fetch=fetch)
    assert gh.status == "ok"
    assert len(fetch.calls) <= 6          # was 25: 1 listing + 12 per repo
    assert gh.requests_made == len(fetch.calls)
    assert sum("/contents/" in u for u in fetch.calls) == 0      # trees replace the 9 contents calls
    assert sum("/git/trees/" in u for u in fetch.calls) == 1     # one per analyzed repo; forks skipped


async def test_fork_is_not_opened_but_is_still_flagged_from_the_listing():
    fetch = make_fetch("priya")
    gh = await collect_github("priya", fetch=fetch)
    clone = next(r for r in gh.repos if r.name == "old-tutorial-clone")
    assert not clone.analyzed
    assert not any("old-tutorial-clone" in u for u in fetch.calls)
    assert "fork_claimed_as_own" in clone.flags and "tutorial_clone" in clone.flags
    # a skipped fork is never skill evidence, even though its topics/language might match
    assert judge_skill_claim("JavaScript", gh, required=False)["status"] == "UNSUPPORTED"


async def test_trees_api_finds_manifests_tests_and_ci():
    world = make_world(extra_paths={"ledger-service": ["tests/test_a.py", ".github/workflows/ci.yml"]})
    gh = await collect_github("priya", fetch=make_fetch("priya", world=world))
    ledger = next(r for r in gh.repos if r.name == "ledger-service")
    assert ledger.manifests_found == ["requirements.txt", "Dockerfile"]
    assert ledger.has_tests and ledger.has_ci


async def test_truncated_tree_falls_back_to_root_listing():
    fetch = make_fetch("priya", trees_truncated={"ledger-service"})
    gh = await collect_github("priya", fetch=fetch)
    ledger = next(r for r in gh.repos if r.name == "ledger-service")
    assert gh.status == "ok" and ledger.tree_truncated
    assert ledger.manifests_found == ["requirements.txt", "Dockerfile"]
    assert sum("/git/trees/" in u for u in fetch.calls) == 2


async def test_thirty_repo_user_is_capped_and_stops_when_skills_are_covered():
    repos = [{"name": f"r{i}", "fork": False, "created_at": "2022-01-01T00:00:00Z",
              "pushed_at": f"2024-01-{i % 28 + 1:02d}T00:00:00Z", "topics": [],
              "default_branch": "main", "language": "Python", "description": ""} for i in range(30)]
    me = {"author": {"login": "u"}, "commit": {"author": {"email": ""}}}
    world = make_world(repos, languages={r["name"]: {"Python": 1} for r in repos},
                       commits={r["name"]: [me] * 3 for r in repos},
                       manifests={r["name"]: {"requirements.txt"} for r in repos},
                       readme={r["name"]: "x" for r in repos}, extra_paths={})
    covered = make_fetch("u", world=world)
    gh = await collect_github("u", fetch=covered, skills=["Python"])
    assert gh.status == "ok" and len(covered.calls) == 5           # listing + one repo, then stop
    capped = make_fetch("u", world=world)
    gh = await collect_github("u", fetch=capped, skills=["Python", "Rust"])
    assert len(capped.calls) <= 33                                  # was 361
    assert gh.status == "partial" or gh.status == "ok"


async def test_second_collect_with_cache_makes_zero_network_requests(tmp_path):
    cache = Cache(tmp_path / "c.db", ttl_hours=24)
    inner = make_fetch("priya")
    first = await collect_github("priya", fetch=cached_fetch(inner, cache))
    n_first = len(inner.calls)
    assert n_first > 0
    second = await collect_github("priya", fetch=cached_fetch(inner, cache))
    assert len(inner.calls) == n_first           # zero new network requests
    assert [r.name for r in second.repos] == [r.name for r in first.repos]
    assert second.repos[0].authorship_ratio == first.repos[0].authorship_ratio


async def test_entry_past_ttl_is_refetched(tmp_path):
    now = [1_000_000.0]
    cache = Cache(tmp_path / "c.db", ttl_hours=1, clock=lambda: now[0])
    inner = make_fetch("priya")
    await collect_github("priya", fetch=cached_fetch(inner, cache))
    n = len(inner.calls)
    now[0] += 30 * 60
    await collect_github("priya", fetch=cached_fetch(inner, cache))
    assert len(inner.calls) == n                 # still fresh
    now[0] += 31 * 60                            # now 61 minutes old
    await collect_github("priya", fetch=cached_fetch(inner, cache))
    assert len(inner.calls) == 2 * n             # expired, so everything was refetched


async def test_token_never_appears_in_cache_db(tmp_path):
    db = tmp_path / "c.db"
    secret = "ghp_SUPERSECRETTOKEN123"
    seen = []
    inner = make_fetch("priya")

    async def spy(url, headers):
        seen.append(headers)
        return await inner(url, headers)

    await collect_github("priya", token=secret, fetch=cached_fetch(spy, Cache(db)))
    assert any(secret in str(h) for h in seen)   # the token really was sent to the network layer
    dump = "\n".join(sqlite3.connect(db).iterdump()) + db.read_bytes().decode("latin-1")
    assert secret not in dump and "Authorization" not in dump and "Bearer" not in dump


async def test_cache_does_not_store_rate_limit_responses(tmp_path):
    cache = Cache(tmp_path / "c.db")
    fetch = cached_fetch(make_fetch_with_headers(rate_limit_after=0), cache)
    status, _body, *_ = await fetch("https://api.github.com/users/x/repos?per_page=100&sort=pushed", {})
    assert status == 403
    assert cache.get("https://api.github.com/users/x/repos?per_page=100&sort=pushed") == (False, None)


async def test_rate_limit_on_repo_two_of_three_is_partial_and_unverifiable_not_unsupported():
    # Rank by recency (no skills given): alpha, beta, gamma. The limit hits while reading beta.
    fetch = make_fetch_with_headers("priya", world=_three_repo_world(), rate_limit_on="/repos/priya/beta/")
    gh = await collect_github("priya", fetch=fetch)
    assert gh.status == "partial"
    assert "rate limit" in gh.partial_reason.lower()
    by_name = {r.name: r for r in gh.repos}
    assert by_name["alpha"].data_complete
    assert not by_name["beta"].data_complete and not by_name["gamma"].data_complete
    # The skill beta would have shown is "could not check", not "checked and found nothing".
    assert judge_skill_claim("Go", gh, required=True)["status"] == "UNVERIFIABLE"
    assert judge_skill_claim("Java", gh, required=False)["status"] == "UNVERIFIABLE"
    assert judge_project_claim("A payments gateway", gh)["status"] == "UNVERIFIABLE"
    # Positive evidence found before the limit is still honoured.
    assert judge_skill_claim("Python", gh, required=True)["status"] == "VERIFIED"


async def test_transport_error_mid_collection_is_partial_too():
    fetch = make_fetch_with_headers("priya", world=_three_repo_world(), raise_on="/repos/priya/beta/languages")
    gh = await collect_github("priya", fetch=fetch)
    assert gh.status == "partial" and "transport error" in gh.partial_reason
    assert judge_skill_claim("Rust", gh, required=False)["status"] == "UNVERIFIABLE"


async def test_genuinely_absent_skill_is_still_unsupported_with_full_data():
    gh = await collect_github("priya", fetch=make_fetch_with_headers("priya", world=_three_repo_world()))
    assert gh.status == "ok" and not gh.incomplete_repos
    assert judge_skill_claim("Rust", gh, required=True)["status"] == "UNSUPPORTED"
    assert judge_project_claim("A payments gateway", gh)["status"] == "UNSUPPORTED"


async def test_remaining_zero_stops_further_requests():
    fetch = make_fetch_with_headers("priya", world=_three_repo_world(), remaining_start=3)
    gh = await collect_github("priya", fetch=fetch)
    # replies carry Remaining 3, 2, 1, 0 ...; the reserve of 1 means we stop at 1, not at 429
    assert len(fetch.calls) == 3
    assert gh.status == "partial" and gh.rate_limit_remaining == 1
    zero = make_fetch_with_headers("priya", world=_three_repo_world(), remaining_start=1)
    gh0 = await collect_github("priya", fetch=zero)
    assert len(zero.calls) == 1 and gh0.status == "partial"


async def test_collector_without_headers_still_works_for_old_fetches():
    gh = await collect_github("priya", fetch=make_fetch("priya"))
    assert gh.status == "ok" and gh.rate_limit_remaining is None


def test_recruiter_summary_says_partial_is_not_evidence_against_candidate():
    import asyncio

    fetch = make_fetch_with_headers("priya", world=_three_repo_world(), rate_limit_on="/repos/priya/beta/")
    gh = asyncio.run(collect_github("priya", fetch=fetch))
    text = _recruiter_summary(gh, [{"status": "UNVERIFIABLE"}], 0.5, 1)
    assert "partly checked" in text and "not evidence against the candidate" in text


async def test_engine_maps_partial_to_error_in_sources_used_and_keeps_detail():
    from app.modules.authenticity_engine.engine import assess

    resume = "Skills: Python, Rust"
    parsed = {"skills": ["Python", "Rust"], "projects": [], "education": [],
              "certifications": [], "experience": []}
    fetch = make_fetch_with_headers("priya", world=_three_repo_world(), rate_limit_on="/repos/priya/beta/")
    out = await assess("c1", resume, parsed, ["Rust"], sources={"github": "priya"}, github_fetch=fetch)
    assert out["sources_used"]["github"] == "error"
    assert out["source_details"]["github"]["status"] == "partial"
    assert "not evidence against the candidate" in out["recruiter_summary"]
    rust = [c for c in out["claims"] if c["text"] == "Rust"]
    assert rust and rust[0]["status"] == "UNVERIFIABLE"
