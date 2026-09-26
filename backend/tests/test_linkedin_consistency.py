"""Module B: LinkedIn collector and Stage 4 role-overlap / cross-checks."""
from app.modules.authenticity_engine.collectors.linkedin import (LinkedInEvidence, collect_linkedin,
                                                                  parse_pdf_export_text,
                                                                  parse_structured_json)
from app.modules.authenticity_engine.consistency import check_linkedin_consistency, check_role_overlap
from app.modules.authenticity_engine.matching import judge_role_claim


def test_structured_json_parses_roles():
    li = collect_linkedin({"type": "structured_json", "content": {
        "roles": [{"title": "Backend Engineer", "company": "Acme", "start": "2020", "end": "Present"}],
        "skills": ["Python"], "education": ["B.S. Computer Science"],
    }})
    assert li.status == "ok"
    assert li.roles[0].title == "Backend Engineer"


def test_missing_linkedin_is_missing_not_error():
    assert collect_linkedin(None).status == "missing"
    assert collect_linkedin({"type": "structured_json", "content": None}).status == "missing"


def test_malformed_json_degrades_to_error_not_a_crash():
    li = collect_linkedin({"type": "structured_json", "content": {"roles": [{"start": object()}]}})
    assert li.status in ("error", "ok")  # never raises


def test_pdf_export_text_finds_dated_roles():
    text = "Acme Corp\nBackend Engineer\n2020 - Present\n\nOldCo\nJunior Dev\n2018 - 2020"
    li = parse_pdf_export_text(text)
    assert li.status == "ok"
    assert len(li.roles) == 2


def test_role_overlap_detected_from_resume_alone():
    exp = [
        {"title": "Senior Backend Engineer", "start": "2019", "end": "2026"},
        {"title": "Backend Engineer", "start": "2021", "end": "Present"},
    ]
    out = check_role_overlap(exp)
    assert len(out) == 1
    assert out[0]["type"] == "overlapping_roles"


def test_no_overlap_for_sequential_roles():
    exp = [
        {"title": "A", "start": "2018", "end": "2020"},
        {"title": "B", "start": "2020", "end": "2022"},
    ]
    assert check_role_overlap(exp) == []


def test_linkedin_date_conflict_detected():
    exp = [{"title": "Backend Engineer", "company": "Acme", "start": "2018", "end": "2020"}]
    from app.modules.authenticity_engine.collectors.linkedin import LinkedInRole
    li = LinkedInEvidence(status="ok", roles=[
        LinkedInRole(title="Backend Engineer", company="Acme", start="2020", end="2022")
    ])
    out = check_linkedin_consistency(exp, li)
    assert any(c["type"] == "date_conflict" for c in out)


def test_linkedin_no_source_yields_no_findings():
    exp = [{"title": "Backend Engineer", "company": "Acme", "start": "2018", "end": "2020"}]
    assert check_linkedin_consistency(exp, LinkedInEvidence(status="missing")) == []


def test_role_claim_corroborated_from_linkedin():
    from app.modules.authenticity_engine.collectors.linkedin import LinkedInRole
    li = LinkedInEvidence(status="ok", roles=[
        LinkedInRole(title="Backend Engineer", company="Acme", start="2020", end="Present")
    ])
    j = judge_role_claim("Backend Engineer", li)
    assert j["status"] == "CORROBORATED"  # never VERIFIED - both self-reported


def test_role_claim_unverifiable_with_no_linkedin():
    j = judge_role_claim("Backend Engineer", LinkedInEvidence(status="missing"))
    assert j["status"] == "UNVERIFIABLE"
