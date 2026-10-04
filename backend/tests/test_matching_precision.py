"""Matching precision and judge-confidence behaviour (Module B Stage 3)."""
from app.modules.authenticity_engine.collectors.github import GitHubEvidence, RepoEvidence
from app.modules.authenticity_engine.collectors.portfolio import PortfolioEvidence
from app.modules.authenticity_engine.matching import (
    authenticity_flags,
    judge_project_claim,
    judge_skill_claim,
    judge_skill_claim_with_portfolio,
)


def repo(name, *, fork=False, mine=10, total=10, analyzed=True, langs=None, readme="",
         flags=None, manifests=None, topics=None, **kw):
    return RepoEvidence(
        name=name, owned=True, fork=fork, commits_by_candidate=mine, commits_total=total,
        analyzed=analyzed, languages=langs or {}, readme_excerpt=readme, flags=flags or [],
        manifests_found=manifests or [], topics=topics or [], **kw,
    )


def gh(*repos, status="ok"):
    return GitHubEvidence(username="u", status=status, repos=list(repos))


def test_generic_word_does_not_verify_a_project():
    g = gh(repo("ledger-pipeline", langs={"Python": 100}))
    r = judge_project_claim("Telemetry pipeline", g)
    assert r["status"] == "UNSUPPORTED"
    assert r["evidence"] == []


def test_real_name_correspondence_still_verifies():
    g = gh(repo("telemetry-pipeline"))
    assert judge_project_claim("Telemetry pipeline", g)["status"] == "VERIFIED"
    g2 = gh(repo("billing-dashboard"))
    assert judge_project_claim("Billing dashboard in JavaScript backed by Docker", g2)["status"] == "VERIFIED"


def test_tech_named_repo_does_not_match_a_description_mentioning_it():
    g = gh(repo("docker", analyzed=False))
    assert judge_project_claim("Billing dashboard in JavaScript backed by Docker", g)["status"] == "UNSUPPORTED"


def test_authored_repo_wins_over_same_named_fork():
    fork = repo("awesome-python", fork=True, mine=0, total=500, analyzed=False)
    own = repo("awesome-python-toolkit", mine=40, total=40)
    for order in ([fork, own], [own, fork]):
        r = judge_project_claim("Awesome python toolkit", gh(*order))
        assert r["status"] == "VERIFIED"
        assert r["evidence"][0]["citation"].endswith("/awesome-python-toolkit")


def test_fork_alone_is_never_verified():
    fork = repo("telemetry-service", fork=True, mine=0, total=300, analyzed=False)
    assert judge_project_claim("Telemetry service", gh(fork))["status"] == "WEAK"


def _claims(skill_text, project_text="", evidence_repo=None):
    out = [{"type": "SKILL", "text": skill_text, "evidence": []}]
    if project_text:
        out.append({"type": "PROJECT", "text": project_text, "evidence": []})
    if evidence_repo:
        out[0]["evidence"] = [{"citation": f"github.com/u/{evidence_repo}"}]
    return out


def test_skill_named_fork_is_not_flagged():
    fork = repo("mysql", fork=True, mine=0, total=900, analyzed=False, flags=["fork_claimed_as_own"])
    g = gh(fork)
    flags = authenticity_flags(g, _claims("MySQL", "Built an analytics platform on MySQL"), ["Analytics Platform"])
    assert flags == []


def test_project_claiming_the_fork_is_flagged():
    fork = repo("flask-tutorial", fork=True, mine=0, total=50, analyzed=False,
                flags=["fork_claimed_as_own"])
    g = gh(fork)
    flags = authenticity_flags(g, _claims("Flask", "Flask tutorial web app"), ["Flask Tutorial"])
    assert [f["flag"] for f in flags] == ["fork_claimed_as_own"]


def test_fork_cited_as_evidence_is_flagged():
    fork = repo("mysql", fork=True, mine=0, total=900, analyzed=False, flags=["fork_claimed_as_own"])
    flags = authenticity_flags(gh(fork), _claims("MySQL", evidence_repo="mysql"))
    assert len(flags) == 1


def test_confidence_monotone_in_evidence_strength():
    def conf(*repos, status="ok"):
        return judge_skill_claim("Python", gh(*repos, status=status), False)["judge_confidence"]

    manifest = conf(repo("a", mine=5, total=10, manifests=["requirements.txt"]))
    lang = conf(repo("a", mine=5, total=10, langs={"Python": 10}))
    high_auth = conf(repo("a", mine=10, total=10, langs={"Python": 10}))
    two = conf(repo("a", mine=10, total=10, langs={"Python": 10}), repo("b", langs={"Python": 5}))
    partial = conf(repo("a", mine=10, total=10, langs={"Python": 10}), status="partial")
    assert manifest == lang < high_auth <= two  # both may sit at the 0.95 ceiling
    assert partial < high_auth
    assert all(0.0 <= c <= 1.0 for c in (manifest, lang, high_auth, two, partial))


def test_readme_only_mention_is_weak_not_verified():
    """Spec 11 Stage 3: VERIFIED only from code/manifests. README prose or a topic label is
    the candidate describing their own work: corroboration (WEAK), not verification."""
    readme = judge_skill_claim("Python", gh(repo("a", mine=5, total=10, readme="python")), False)
    topic = judge_skill_claim("Python", gh(repo("a", mine=5, total=10, topics=["python"])), False)
    code = judge_skill_claim("Python", gh(repo("a", mine=5, total=10, langs={"Python": 10})), False)
    assert readme["status"] == "WEAK" and topic["status"] == "WEAK"
    assert code["status"] == "VERIFIED"
    assert readme["judge_confidence"] < code["judge_confidence"]


def test_confidence_absence_grows_with_examined_repos_but_is_capped():
    def conf(n):
        return judge_skill_claim("Rust", gh(*[repo(f"r{i}", langs={"Go": 1}) for i in range(n)]), False)["judge_confidence"]

    assert conf(0) < conf(1) < conf(5) < 0.9


def test_confidence_not_a_per_status_constant():
    a = judge_skill_claim("Python", gh(repo("a", langs={"Python": 1})), False)
    b = judge_skill_claim("Python", gh(repo("a", mine=4, total=10, manifests=["requirements.txt"])), False)
    assert a["status"] == b["status"] == "VERIFIED"
    assert a["judge_confidence"] != b["judge_confidence"]


def test_partial_still_unverifiable_when_nothing_found():
    g = gh(repo("a", data_complete=False), status="partial")
    assert judge_skill_claim("Rust", g, False)["status"] == "UNVERIFIABLE"
    assert judge_project_claim("Nothing alike", g)["status"] == "UNVERIFIABLE"


def test_portfolio_upgrades_to_weak_when_github_missing():
    missing = GitHubEvidence(username="u", status="missing")
    pf = PortfolioEvidence(url="https://x.dev", status="ok", tech_mentions=["Rust"])
    r = judge_skill_claim_with_portfolio("Rust", missing, pf, False)
    assert r["status"] == "WEAK" and r["evidence"][0]["source"] == "portfolio"


def test_portfolio_does_not_override_unsupported_from_real_github():
    pf = PortfolioEvidence(url="https://x.dev", status="ok", tech_mentions=["Rust"])
    r = judge_skill_claim_with_portfolio("Rust", gh(repo("a", langs={"Go": 1})), pf, False)
    assert r["status"] == "UNSUPPORTED"
