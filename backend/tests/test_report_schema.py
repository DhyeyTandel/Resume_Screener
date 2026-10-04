"""The unified report schema (Spec 13): one Pydantic model for the whole report."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.pipeline.orchestrator as orch
from app.llm.client import LLMClient
from app.pipeline.orchestrator import screen_candidate
from app.schemas.export_schema import OUT, build_schema
from app.schemas.report import UnifiedReport, VerificationGapV1
from tests.fixtures.github_fixtures import make_fetch

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "sample_data"
PDFS = Path(__file__).resolve().parent / "fixtures" / "pdfs"
JD = (SAMPLES / "jd_backend_engineer.txt").read_text()
RESUMES = {p.stem: p.read_text() for p in sorted((SAMPLES / "resumes").glob("*.txt"))}
SAMPLE_OUTPUTS = sorted((ROOT / "sample_output").glob("*.json"))


def _assert_valid(report: dict) -> None:
    UnifiedReport.model_validate(report)
    meta = report["extensions"]["meta"]
    assert meta["schema_valid"] is True, meta.get("schema_errors")
    assert "schema_errors" not in meta


async def _run(**kw) -> dict:
    return await screen_candidate(jd_text=JD, llm=LLMClient("mock"), **kw)


def test_sample_outputs_exist():
    assert len(SAMPLE_OUTPUTS) >= 6


@pytest.mark.parametrize("path", SAMPLE_OUTPUTS, ids=lambda p: p.name)
def test_every_sample_output_validates(path):
    UnifiedReport.model_validate(json.loads(path.read_text()))


@pytest.mark.parametrize("name", list(RESUMES))
async def test_normal_path_reports_validate(name):
    _assert_valid(await _run(pasted_text=RESUMES[name], candidate_name="Test Candidate"))


async def test_normal_path_with_github_evidence_validates():
    r = await _run(pasted_text=RESUMES["01_strong_match"], github_username="priya",
                   consent={"github": True}, github_fetch=make_fetch("priya"))
    assert r["extensions"]["authenticity"]["sources_used"]["github"] == "ok"
    _assert_valid(r)


async def test_hidden_text_attack_report_validates():
    data = (PDFS / "injection_hidden.pdf").read_bytes()
    r = await _run(filename="injection_hidden.pdf", data=data)
    assert r["extensions"]["integrity"]["findings"]
    _assert_valid(r)


async def test_error_path_corrupt_file_validates():
    r = await _run(filename="broken.pdf", data=b"this is not a pdf")
    assert r["extensions"]["status"] == "Error"
    _assert_valid(r)


async def test_no_text_layer_scan_validates():
    r = await _run(filename="scanned_no_text.pdf", data=(PDFS / "scanned_no_text.pdf").read_bytes())
    assert r["extensions"]["status"] == "Not Enough Evidence"
    assert r["extensions"]["authenticity"] is None
    _assert_valid(r)


async def test_all_optional_modules_failing_still_validates(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("module down")

    monkeypatch.setattr(orch, "assess", boom)
    monkeypatch.setattr(orch, "interpret", boom)
    monkeypatch.setattr(orch, "generate_interview_questions", boom)
    r = await _run(pasted_text=RESUMES["01_strong_match"])
    stages = r["extensions"]["meta"]["stages"]
    assert stages["authenticity"]["status"] == "error"
    assert stages["interview"]["status"] == "error"
    assert r["extensions"]["authenticity"] is None
    _assert_valid(r)


async def test_schema_failure_never_crashes_and_is_recorded(monkeypatch):
    def bad_decide(**kw):
        return "Definitely Hire", ["rule"]

    monkeypatch.setattr(orch, "decide", bad_decide)
    r = await _run(pasted_text=RESUMES["01_strong_match"])
    assert r["recommendation"] == "Definitely Hire"  # still returned
    meta = r["extensions"]["meta"]
    assert meta["schema_valid"] is False
    assert any("recommendation" in e for e in meta["schema_errors"])


def test_validate_report_tolerates_a_malformed_report():
    assert orch.validate_report({"candidate_name": "x"})["candidate_name"] == "x"


@pytest.fixture
def good() -> dict:
    return json.loads(SAMPLE_OUTPUTS[0].read_text())


def test_invalid_recommendation_fails(good):
    good["recommendation"] = "Hire Immediately"
    with pytest.raises(ValidationError):
        UnifiedReport.model_validate(good)


def test_human_review_required_false_fails(good):
    good["extensions"]["human_review_required"] = False
    with pytest.raises(ValidationError):
        UnifiedReport.model_validate(good)


def test_extra_top_level_key_is_forbidden_but_extension_extras_allowed(good):
    extra_ext = copy.deepcopy(good)
    extra_ext["extensions"]["something_new"] = {"ok": True}
    UnifiedReport.model_validate(extra_ext)
    good["summry"] = "typo"
    with pytest.raises(ValidationError):
        UnifiedReport.model_validate(good)


def test_bad_enums_fail(good):
    for mutate in (
        lambda r: r["ai_text_indicators"].__setitem__("level", "Severe"),
        lambda r: r["requirement_match"][0].__setitem__("status", "Maybe"),
        lambda r: r["extensions"]["authenticity"].__setitem__("band", "FRAUD"),
        lambda r: r["extensions"]["authenticity"]["claims"][0].__setitem__("epistemic_tag", "GUESS"),
        lambda r: r["extensions"]["authenticity"]["sources_used"].__setitem__("github", "maybe"),
        lambda r: r["extensions"]["authenticity"]["verification_gaps"][0].__setitem__("priority", "urgent"),
    ):
        bad = copy.deepcopy(good)
        mutate(bad)
        with pytest.raises(ValidationError):
            UnifiedReport.model_validate(bad)


def test_verification_gap_v1_contract(good):
    assert set(VerificationGapV1.model_fields) == {"claim_id", "what_to_verify", "priority"}
    with pytest.raises(ValidationError):
        VerificationGapV1.model_validate(
            {"claim_id": "C1", "what_to_verify": "x", "priority": "high", "surprise": 1}
        )
    assert UnifiedReport.model_validate(good).extensions.authenticity.verification_gaps_contract == (
        "verification_gaps.v1"
    )


async def test_pipeline_tags_verification_gaps_contract():
    r = await _run(pasted_text=RESUMES["01_strong_match"])
    assert r["extensions"]["authenticity"]["verification_gaps_contract"] == "verification_gaps.v1"


def test_json_schema_exports_and_committed_file_is_current():
    schema = build_schema()
    assert schema["title"] == "UnifiedReport"
    assert schema["additionalProperties"] is False
    assert "extensions" in schema["properties"]
    json.dumps(schema)
    assert OUT.exists(), "run: python -m app.schemas.export_schema"
    assert json.loads(OUT.read_text()) == schema, "docs/report.schema.json is stale; re-run the export"


async def test_core_skill_analysis_failing_gives_a_valid_error_report(monkeypatch):
    import app.modules.core_screening.core as core

    def boom(*a, **k):
        raise RuntimeError("skill analysis down")

    monkeypatch.setattr(core, "analyze_skill", boom)
    r = await _run(pasted_text=RESUMES["01_strong_match"])
    assert r["extensions"]["status"] == "Error"
    _assert_valid(r)
