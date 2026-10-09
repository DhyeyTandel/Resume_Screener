"""Module B Stage 6 bands, Stage 3/4 contradictions, achievements and METRIC checks (Spec 11)."""
import pytest

from app.modules.authenticity_engine import engine
from app.modules.authenticity_engine.collectors.github import GitHubEvidence, RepoEvidence
from app.modules.authenticity_engine.collectors.linkedin import LinkedInEvidence, LinkedInRole
from app.modules.authenticity_engine.consistency import (
    check_linkedin_consistency,
    compare_titles,
    normalise_title,
)

BANDS = {"min_confidence": 0.40, "high_trust": 0.75, "moderate": 0.50}


def repo(name="svc", readme="", langs=("python", "docker"), *, mine=30, total=30, fork=False):
    return RepoEvidence(
        name=name, owned=True, fork=fork, languages={lang: 100 for lang in langs}, readme_excerpt=readme,
        commits_by_candidate=mine, commits_total=total, analyzed=True,
    )


def gh_with(*repos):
    return GitHubEvidence(username="dev", status="ok", repos=list(repos))


def parsed_resume(skills=("Python",), experience=(), projects=(), achievements=(), education=()):
    return {
        "skills": list(skills),
        "experience": [dict(e, relevant_points=list(e.get("relevant_points", []))) for e in experience],
        "projects": list(projects), "education": list(education), "certifications": [],
        "achievements": list(achievements),
    }


def resume_text(parsed):
    lines = ["Experience"]
    for e in parsed["experience"]:
        lines.append(f"{e['title']} at {e['company']}, {e['start']} - {e['end']}")
        lines += [f"• {p}" for p in e["relevant_points"]]
    lines += ["Skills", ", ".join(parsed["skills"]), "Achievements"] + [f"• {a}" for a in parsed["achievements"]]
    return "\n".join(lines)


async def run(parsed, gh=None, linkedin=None, required=("Python",), monkeypatch=None):
    if monkeypatch is not None and gh is not None:
        async def fake(*a, **k):
            return gh
        monkeypatch.setattr(engine, "collect_github", fake)
    sources = {}
    if gh is not None:
        sources["github"] = "dev"
    if linkedin is not None:
        sources["linkedin"] = {"type": "structured_json", "content": {"roles": linkedin}}
    return await engine.assess("c1", resume_text(parsed), parsed, list(required), sources=sources)


def claim(out, ctype, startswith=""):
    return next(c for c in out["claims"] if c["type"] == ctype and c["text"].startswith(startswith))


ROLE = {"title": "Software Engineer", "company": "Acme", "start": "2020", "end": "2022"}


# ---------------------------------------------------------------- Defect 1: bands
def test_band_thresholds_apply_to_the_authenticity_score_not_confidence():
    # syn-0079-like: every checkable claim verified, authenticity 0.965, modest confidence.
    assert engine.assign_band(0.55, 0.965, False, BANDS) == "HIGH_TRUST"
    assert engine.assign_band(0.45, 0.60, False, BANDS) == "MODERATE"
    assert engine.assign_band(0.90, 0.30, False, BANDS) == "NEEDS_VERIFICATION"


def test_confidence_floor_comes_first_then_contradiction_forcing():
    assert engine.assign_band(0.39, 0.99, False, BANDS) == "INSUFFICIENT_EVIDENCE"
    assert engine.assign_band(0.39, 0.99, True, BANDS) == "INSUFFICIENT_EVIDENCE"  # A-6c
    assert engine.assign_band(0.80, 0.99, True, BANDS) == "NEEDS_VERIFICATION"


async def test_all_verified_profile_with_middling_confidence_is_not_needs_verification(monkeypatch):
    parsed = parsed_resume(skills=("Python", "Docker"), achievements=["Won the internal hackathon, 2023"])
    out = await run(parsed, gh_with(repo()), monkeypatch=monkeypatch, required=("Python",))
    s = out["scores"]
    assert s["reliability"] == 1.0
    assert s["authenticity"] >= 0.75 and s["assessment_confidence"] < 0.75
    assert out["band"] == "HIGH_TRUST"


async def test_unsupported_heavy_profile_is_not_high_trust(monkeypatch):
    skills = ("Python", "Rust", "Scala", "Haskell", "Kotlin", "Swift")
    parsed = parsed_resume(skills=skills)
    out = await run(parsed, gh_with(repo(langs=("python",))), monkeypatch=monkeypatch,
                    required=("Rust",))
    assert sum(c["status"] == "UNSUPPORTED" for c in out["claims"]) >= 4
    assert out["band"] != "HIGH_TRUST"
    assert out["scores"]["authenticity"] < 0.75


# ---------------------------------------------------------------- Defect 3: titles
def test_engineer_developer_programmer_and_abbreviations_are_equivalent():
    assert compare_titles("Software Engineer", "Software Developer") is None
    assert compare_titles("Sr. SWE", "Senior Software Developer") is None
    assert compare_titles("Front-end Developer", "Frontend Engineer") is None
    assert normalise_title("Jr. Programmer") == ["junior", "engineer"]


@pytest.mark.parametrize("resume,li", [
    ("Senior Software Engineer", "Software Engineer"),
    ("Lead Backend Engineer", "Backend Developer"),
    ("Staff Backend Engineer", "Backend Engineer"),
    ("Principal Data Engineer", "Data Engineer"),
    ("Engineering Manager", "Engineer"),
])
def test_seniority_inflation_is_flagged(resume, li):
    assert compare_titles(resume, li) == "seniority_inflation"


@pytest.mark.parametrize("resume,li", [
    ("Software Engineer", "Senior Software Engineer"),
    ("Backend Developer", "Lead Backend Engineer"),
    ("Software Engineer", "Staff Software Engineer"),
])
def test_lower_rank_on_the_resume_is_not_a_contradiction(resume, li):
    assert compare_titles(resume, li) is None


def test_genuinely_different_title_is_still_a_mismatch():
    assert compare_titles("Data Engineer", "Marketing Analyst") == "different_role"


def test_linkedin_findings_carry_the_role_reference():
    li = LinkedInEvidence(status="ok", roles=[LinkedInRole("Software Engineer", "Acme", "2020", "2022")])
    exp = [dict(ROLE, title="Senior Software Engineer")]
    f = check_linkedin_consistency(exp, li)
    assert f[0]["type"] == "title_mismatch"
    assert f[0]["citation"] == "linkedin_export:role:Software Engineer"
    assert check_linkedin_consistency([dict(ROLE, title="Software Developer")], li) == []


# ---------------------------------------------------------------- Defect 2: ROLE claim status
async def test_date_conflict_marks_the_role_claim_contradicted_and_forces_review(monkeypatch):
    parsed = parsed_resume(skills=("Python", "Docker"), experience=[ROLE])
    li_roles = [{"title": "Software Engineer", "company": "Acme", "start": "2018", "end": "2019"}]
    out = await run(parsed, gh_with(repo()), li_roles, monkeypatch=monkeypatch)
    role = claim(out, "ROLE")
    assert role["status"] == "CONTRADICTED"
    assert role["evidence"][0]["citation"] == "linkedin_export:role:Software Engineer"
    assert not any(w in role["rationale"].lower() for w in ("fraud", "lied", "false", "dishonest"))
    assert any(c["type"] == "date_conflict" for c in out["contradictions"])
    assert out["scores"]["assessment_confidence"] >= 0.40
    assert out["band"] == "NEEDS_VERIFICATION"


async def test_seniority_inflation_marks_role_contradicted(monkeypatch):
    parsed = parsed_resume(experience=[dict(ROLE, title="Staff Software Engineer")])
    li_roles = [{"title": "Software Engineer", "company": "Acme", "start": "2020", "end": "2022"}]
    out = await run(parsed, gh_with(repo()), li_roles, monkeypatch=monkeypatch)
    assert claim(out, "ROLE")["status"] == "CONTRADICTED"
    assert out["band"] == "NEEDS_VERIFICATION"


async def test_synonym_title_stays_corroborated(monkeypatch):
    parsed = parsed_resume(experience=[dict(ROLE, title="Software Developer")])
    li_roles = [{"title": "Software Engineer", "company": "Acme", "start": "2020", "end": "2022"}]
    out = await run(parsed, gh_with(repo()), li_roles, monkeypatch=monkeypatch)
    assert claim(out, "ROLE")["status"] == "CORROBORATED"
    assert out["contradictions"] == []
    assert out["band"] != "NEEDS_VERIFICATION"


async def test_resume_internal_overlap_does_not_mark_claims(monkeypatch):
    exp = [dict(ROLE, title="Backend Engineer", company="A", start="2019", end="2024"),
           dict(ROLE, title="Data Engineer", company="B", start="2021", end="2025")]
    li_roles = [{"title": e["title"], "company": e["company"], "start": e["start"], "end": e["end"]}
                for e in exp]
    out = await run(parsed_resume(experience=exp), gh_with(repo()), li_roles, monkeypatch=monkeypatch)
    assert any(c["type"] == "overlapping_roles" for c in out["contradictions"])
    assert all(c["status"] != "CONTRADICTED" for c in out["claims"] if c["type"] == "ROLE")
    # A resume-only finding is not a source-contradicted claim (A-22): it is reported and
    # becomes a verification gap, but it does not force the band (see test_audit_fairness.py).
    assert out["band"] != "NEEDS_VERIFICATION"
    assert all(c["scope"] == "resume_only" for c in out["contradictions"])
    assert any("overlap" in g["what_to_verify"].lower() for g in out["verification_gaps"])


# ---------------------------------------------------------------- Defect 4: empty GitHub
async def test_empty_github_is_missing_and_not_a_source(monkeypatch):
    empty = GitHubEvidence(username="dev", status="missing", error="no public repositories")
    out = await run(parsed_resume(), empty, monkeypatch=monkeypatch)
    assert out["sources_used"]["github"] == "missing"
    assert out["scores"]["assessment_confidence"] < 0.40
    assert out["band"] == "INSUFFICIENT_EVIDENCE"
    assert all(c["status"] == "UNVERIFIABLE" for c in out["claims"])
    assert "reviewed across 0" not in out["recruiter_summary"]


# ---------------------------------------------------------------- Defect 5: achievements
def test_achievements_section_becomes_claims_with_exact_spans():
    parsed = parsed_resume(achievements=["Won the internal hackathon, 2023", "Employee of the quarter, 2024"])
    text = resume_text(parsed)
    claims = [c for c in engine.extract_claims(text, parsed) if c["type"] == "ACHIEVEMENT"]
    assert [c["text"] for c in claims] == parsed["achievements"]
    for c in claims:
        assert text[c["source_span"]["start"]:c["source_span"]["end"]] == c["text"]


async def test_achievements_are_unverifiable_not_unsupported(monkeypatch):
    parsed = parsed_resume(achievements=["Won the internal hackathon, 2023"])
    out = await run(parsed, gh_with(repo()), monkeypatch=monkeypatch)
    assert claim(out, "ACHIEVEMENT")["status"] == "UNVERIFIABLE"


# ---------------------------------------------------------------- Defect 6: METRIC claims
def metric_parsed(point):
    return parsed_resume(experience=[dict(ROLE, relevant_points=[point])])


async def metric_status(point, readme, monkeypatch, **kw):
    out = await run(metric_parsed(point), gh_with(repo(readme=readme, **kw)), monkeypatch=monkeypatch)
    return claim(out, "METRIC"), out


async def test_metric_equal_to_readme_figure_is_verified(monkeypatch):
    m, _ = await metric_status("Rewrote the search query, cutting run time by 32 percent",
                               "# svc\n\nBenchmarks: run time reduced by 32 percent.\n", monkeypatch)
    assert m["status"] == "VERIFIED"
    assert m["evidence"][0]["citation"] == "github.com/dev/svc"


async def test_metric_conflicting_with_readme_is_contradicted_but_does_not_force_the_band(monkeypatch):
    m, out = await metric_status("Rewrote the search query, cutting run time by 96 percent",
                                 "# svc\n\nBenchmarks: run time reduced by 32 percent.\n", monkeypatch)
    assert m["status"] == "CONTRADICTED"
    assert m["evidence"][0]["citation"] == "github.com/dev/svc"
    assert "32" in m["rationale"] and "96" in m["rationale"]
    assert out["contradictions"] == []
    assert out["band"] != "NEEDS_VERIFICATION" or out["scores"]["authenticity"] < 0.75


@pytest.mark.parametrize("readme", [
    "Handles 96 requests per second under load.",          # different quantity type
    "Test coverage is 32 percent.",                          # same unit, unrelated subject
    "Benchmarks: memory usage reduced by 32 percent.",       # same unit, different metric
    "# svc\n\nNo figures here.",
])
async def test_unrelated_readme_numbers_stay_unverifiable(monkeypatch, readme):
    m, _ = await metric_status("Rewrote the search query, cutting run time by 96 percent",
                               readme, monkeypatch)
    assert m["status"] == "UNVERIFIABLE"


async def test_generic_shared_word_confirms_but_never_contradicts(monkeypatch):
    claim_text = "Set up CI/CD workflows that cut release time by 96 percent"
    readme = "Benchmarks: run time reduced by 32 percent."
    m, _ = await metric_status(claim_text, readme, monkeypatch)
    assert m["status"] == "UNVERIFIABLE"  # only 'time' is shared: not enough to accuse
    m, _ = await metric_status(claim_text.replace("96", "32"), readme, monkeypatch)
    assert m["status"] == "VERIFIED"  # equal figure is enough to confirm


async def test_metric_with_two_figures_of_one_type_is_never_contradicted(monkeypatch):
    m, _ = await metric_status("Cut checkout latency from 8 seconds to 5 seconds",
                               "Checkout latency is now 3 seconds.", monkeypatch)
    assert m["status"] == "UNVERIFIABLE"


async def test_metric_ignores_forks_and_barely_authored_repos(monkeypatch):
    readme = "Benchmarks: run time reduced by 32 percent."
    point = "Rewrote the search query, cutting run time by 96 percent"
    m, _ = await metric_status(point, readme, monkeypatch, fork=True)
    assert m["status"] == "UNVERIFIABLE"
    m, _ = await metric_status(point, readme, monkeypatch, mine=1, total=40)
    assert m["status"] == "UNVERIFIABLE"


async def test_metric_without_github_is_unverifiable(monkeypatch):
    out = await run(metric_parsed("Cut run time by 96 percent"))
    assert claim(out, "METRIC")["status"] == "UNVERIFIABLE"
