"""Precision of GitHub skill matching and flags (Spec 11 Stages 2 and 3, Spec 2.4).

No live network: every test uses the injected fixture fetch."""
from app.modules.authenticity_engine.collectors.github import (
    GitHubEvidence,
    RepoEvidence,
    collect_github,
    is_tutorial_clone,
    repo_covers_skill,
)
from app.modules.authenticity_engine.matching import authenticity_flags, judge_skill_claim
from tests.fixtures.github_fixtures import make_fetch, make_world

ME = {"author": {"login": "u"}, "commit": {"author": {"email": ""}}}


def _repo(name="svc", language="Python", **kw):
    raw = {"name": name, "fork": False, "created_at": "2022-01-01T00:00:00Z",
           "pushed_at": "2024-01-01T00:00:00Z", "topics": [], "default_branch": "main",
           "language": language, "description": "", **kw}
    return raw


def _world(repos, *, languages, manifests, readme=None, contents=None):
    names = [r["name"] for r in repos]
    return make_world(
        repos, languages=languages, commits={n: [ME] * 5 for n in names},
        manifests=manifests, readme=readme or {n: "x" for n in names}, extra_paths={},
        manifest_contents=contents)


async def _collect(world, skills):
    fetch = make_fetch("u", world=world)
    return await collect_github("u", fetch=fetch, skills=skills), fetch


async def test_zero_repo_account_is_missing_not_checked_and_found_nothing():
    gh = await collect_github("u", fetch=make_fetch("u", world=make_world([])), skills=["Python"])
    assert gh.status == "missing" and gh.error == "no public repositories" and not gh.has_data
    assert judge_skill_claim("Python", gh, required=True)["status"] == "UNVERIFIABLE"


async def test_all_forks_account_is_missing_for_skills_but_keeps_fork_flags():
    fork = _repo("old-tutorial-clone", "JavaScript", fork=True, created_at="2020-01-02T00:00:00Z",
                 pushed_at="2020-01-01T00:00:00Z", description="Following along with a freecodecamp tutorial.")
    fetch = make_fetch("u", world=make_world([fork]))
    gh = await collect_github("u", fetch=fetch, skills=["JavaScript"])
    assert gh.status == "missing" and "forks" in (gh.error or "")
    assert judge_skill_claim("JavaScript", gh, required=True)["status"] == "UNVERIFIABLE"
    assert len(fetch.calls) == 1                                   # nothing was opened
    flags = {f["flag"] for f in authenticity_flags(gh)}
    assert {"fork_claimed_as_own", "tutorial_clone"} <= flags


async def test_manifest_name_alone_does_not_verify_a_framework():
    repos = [_repo()]
    no_dep = _world(repos, languages={"svc": {"Python": 100}}, manifests={"svc": {"requirements.txt"}},
                    contents={"svc": {"requirements.txt": "requests==2.31\nflask-cors  # not flask\n"}})
    gh, _ = await _collect(no_dep, ["Python", "FastAPI", "Django"])
    assert judge_skill_claim("Python", gh, required=True)["status"] == "VERIFIED"   # language level
    assert judge_skill_claim("FastAPI", gh, required=True)["status"] == "UNSUPPORTED"
    assert judge_skill_claim("Django", gh, required=True)["status"] == "UNSUPPORTED"
    with_dep = _world(repos, languages={"svc": {"Python": 100}}, manifests={"svc": {"requirements.txt"}},
                      contents={"svc": {"requirements.txt": "fastapi[standard]>=0.110\nuvicorn\n"}})
    gh, fetch = await _collect(with_dep, ["FastAPI", "Django"])
    assert judge_skill_claim("FastAPI", gh, required=True)["status"] == "VERIFIED"
    assert judge_skill_claim("Django", gh, required=True)["status"] == "UNSUPPORTED"
    assert sum("/contents/" in u for u in fetch.calls) == 1        # one cached-able request


async def test_package_json_dependencies_prove_react_but_not_vue():
    pkg = '{"dependencies": {"react": "^18"}, "devDependencies": {"typescript": "^5"}}'
    world = _world([_repo("web", "JavaScript")], languages={"web": {"JavaScript": 9}},
                   manifests={"web": {"package.json"}}, contents={"web": {"package.json": pkg}})
    gh, _ = await _collect(world, ["React", "Vue", "TypeScript"])
    assert judge_skill_claim("React", gh, required=True)["status"] == "VERIFIED"
    assert judge_skill_claim("TypeScript", gh, required=True)["status"] == "VERIFIED"
    assert judge_skill_claim("Vue", gh, required=True)["status"] == "UNSUPPORTED"


async def test_unreadable_manifest_content_is_not_proof_and_a_readable_toml_is():
    world = _world([_repo()], languages={"svc": {"Python": 100}}, manifests={"svc": {"pyproject.toml"}})
    gh, _ = await _collect(world, ["FastAPI"])                    # file listed but content 404
    assert judge_skill_claim("FastAPI", gh, required=True)["status"] == "UNSUPPORTED"
    toml = '[project]\ndependencies = ["fastapi>=0.1", "httpx"]\n'
    ok = _world([_repo()], languages={"svc": {"Python": 100}}, manifests={"svc": {"pyproject.toml"}},
                contents={"svc": {"pyproject.toml": toml}})
    gh, _ = await _collect(ok, ["FastAPI"])
    assert judge_skill_claim("FastAPI", gh, required=True)["status"] == "VERIFIED"


def _ev(**kw):
    return RepoEvidence(name="r", owned=True, fork=False, analyzed=True, **kw)


def test_java_does_not_match_javascript_and_short_names_need_whole_tokens():
    js = _ev(languages={"JavaScript": 10}, readme_excerpt="A JavaScript app, not going to Django.")
    assert not repo_covers_skill(js, "Java")
    assert repo_covers_skill(js, "JavaScript") and repo_covers_skill(js, "JS")
    assert not repo_covers_skill(js, "Go")          # 'go' is inside 'going' and 'Django'
    assert not repo_covers_skill(_ev(languages={"TypeScript": 1}), "JavaScript")
    assert repo_covers_skill(_ev(languages={"Go": 1}), "Go")
    assert repo_covers_skill(_ev(languages={"Java": 1}), "Java")
    assert not repo_covers_skill(_ev(languages={"Java": 1}), "JavaScript")


def test_aliases_resolve_through_the_skill_graph():
    assert repo_covers_skill(_ev(topics=["vue"]), "Vue.js")
    assert repo_covers_skill(_ev(readme_excerpt="Built with Vue."), "Vue.js")
    assert repo_covers_skill(_ev(readme_excerpt="Deployed on k8s."), "Kubernetes")
    assert repo_covers_skill(_ev(readme_excerpt="Deployed on Kubernetes."), "K8s")
    assert repo_covers_skill(_ev(topics=["kafka"]), "Apache Kafka")
    assert repo_covers_skill(_ev(topics=["vue-js"]), "Vue")
    assert not repo_covers_skill(_ev(topics=["vuex"]), "Vue")


def test_one_mention_of_tutorial_is_not_a_clone_but_a_real_clone_is():
    assert not is_tutorial_clone("# scheduler-service\n\nSee the tutorial section below for local setup.")
    assert not is_tutorial_clone("Thanks to the Udemy course for the idea.")
    assert not is_tutorial_clone("Includes starter code and exercise 3 notes.")
    assert is_tutorial_clone("Following along with a freecodecamp tutorial project.")
    assert is_tutorial_clone("# shop\nBuilt while following along with a Udemy tutorial.")


async def test_genuine_repo_with_one_tutorial_word_is_not_flagged_end_to_end():
    readme = {"svc": "# svc\n\nA scheduler.\n\nSee the tutorial section below for local setup.\n"}
    world = _world([_repo()], languages={"svc": {"Python": 1}}, manifests={"svc": set()}, readme=readme)
    gh, _ = await _collect(world, ["Python"])
    assert gh.repos[0].flags == []
    fixture = await collect_github("priya", fetch=make_fetch("priya"))
    clone = next(r for r in fixture.repos if r.name == "old-tutorial-clone")
    assert "tutorial_clone" in clone.flags                          # the real clone stays flagged


async def test_framework_claims_stay_within_the_request_budget():
    repos = [_repo(f"r{i}") for i in range(30)]
    manifests = {r["name"]: {"requirements.txt", "pyproject.toml"} for r in repos}
    world = _world(repos, languages={r["name"]: {"Python": 1} for r in repos}, manifests=manifests)
    gh, fetch = await _collect(world, ["Python", "FastAPI", "Django", "Flask", "Rust"])
    assert len(fetch.calls) <= 45                                   # the configured hard cap
    assert sum("/contents/" in u for u in fetch.calls) <= 6         # separate manifest-read cap
    # without a framework claim there are no content requests at all (the A-18 budget holds)
    plain, fetch2 = await _collect(world, ["Python", "Rust"])
    assert len(fetch2.calls) <= 33 and sum("/contents/" in u for u in fetch2.calls) == 0
    assert isinstance(plain, GitHubEvidence)

