"""Module A against real PDF files built by tests/fixtures/make_pdfs.py."""
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")

from app.llm.client import LLMClient
from app.modules.integrity_guard.scanner import scan
from app.parsing import loader
from app.parsing.loader import NoTextLayer, UnreadableFile, load
from app.pipeline.orchestrator import screen_candidate

PDFS = Path(__file__).parent / "fixtures" / "pdfs"
JD = (Path(__file__).resolve().parents[2] / "sample_data" / "jd_backend_engineer.txt").read_text()


def run(name: str):
    doc = load(name, (PDFS / name).read_bytes())
    return doc, scan(doc, JD)


def codes(report) -> set[str]:
    return {f["code"] for f in report["flags"]}


def test_clean():
    doc, r = run("clean.pdf")
    assert r["verdict"] == "clean" and r["penalty"] == 1.0
    assert r["flags"] == []
    assert "FastAPI" in doc.visible_text


@pytest.mark.parametrize("name", ["white_text.pdf", "tiny_font.pdf", "offpage.pdf"])
def test_hidden_text_variants(name):
    doc, r = run(name)
    assert "HIDDEN_TEXT" in codes(r)
    assert r["verdict"] == "suspicious" and r["penalty"] == 0.75
    assert "Kubernetes" not in doc.visible_text and "Google" not in doc.visible_text
    assert "FastAPI" in doc.visible_text


def test_injection_hidden():
    doc, r = run("injection_hidden.pdf")
    assert {"HIDDEN_TEXT", "INJECTION_HIDDEN"} <= codes(r)
    assert r["verdict"] == "attack" and r["penalty"] == 0.40
    assert "ignore all previous" not in doc.visible_text.lower()
    assert "10/10" not in doc.visible_text
    assert "ignore all previous" in r["hidden_text"].lower()


def test_jd_clone_hidden():
    doc, r = run("jd_clone_hidden.pdf")
    assert {"HIDDEN_TEXT", "JD_CLONE"} <= codes(r)
    assert r["verdict"] == "attack"
    assert "Backend" not in doc.visible_text.replace("Backend Engineer |", "")


def test_metadata_stuffed():
    _, r = run("metadata_stuffed.pdf")
    assert "METADATA_STUFF" in codes(r)
    assert r["verdict"] == "suspicious"


def test_ocr_layer():
    doc, r = run("ocr_layer.pdf")
    assert codes(r) <= {"OCR_LAYER"}
    assert r["verdict"] == "clean" and r["penalty"] == 1.0
    assert "OCR_LAYER" in codes(r)
    assert doc.visible_text  # scanned text is still usable downstream


def test_no_parser_divergence_on_clean_and_hidden():
    # Both parsers extract hidden text too, so they agree; hidden text is caught
    # by HIDDEN_TEXT, not by parser disagreement.
    for name in ("clean.pdf", "white_text.pdf", "tiny_font.pdf", "injection_hidden.pdf"):
        assert "PARSER_DIVERGENCE" not in codes(run(name)[1]), name


# --- scanned PDF with no text layer (Spec 7) -------------------------------------------

BASE_KEYS = (
    "candidate_name", "overall_match_score", "recommendation", "summary", "ai_text_indicators",
    "requirement_match", "matched_skills", "missing_or_unclear_skills", "technical_gaps",
    "education_assessment", "experience_assessment", "strengths", "risks",
    "interview_questions", "fairness_notice", "extensions",
)


@pytest.fixture
def no_ocr(monkeypatch):
    """Pin the no-OCR path so the result does not depend on the machine's tesseract."""
    monkeypatch.setattr(loader, "_ocr_pdf", lambda data: None)


def test_scanned_pdf_raises_no_text_layer(no_ocr):
    with pytest.raises(NoTextLayer) as e:
        load("scanned_no_text.pdf", (PDFS / "scanned_no_text.pdf").read_bytes())
    assert isinstance(e.value, UnreadableFile)
    assert "No text layer" in e.value.reason


def test_ocr_text_is_used_when_ocr_is_available(monkeypatch):
    monkeypatch.setattr(loader, "_ocr_pdf", lambda data: "Priya Sharma\nBuilt REST APIs in Python")
    doc = load("scanned_no_text.pdf", (PDFS / "scanned_no_text.pdf").read_bytes())
    assert doc.ocr_used and "REST APIs" in doc.visible_text
    assert scan(doc, JD)["verdict"] == "clean"


def test_ocr_is_skipped_cleanly_without_pytesseract(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pytesseract", None)  # import raises ImportError
    assert loader._ocr_pdf((PDFS / "scanned_no_text.pdf").read_bytes()) is None


async def test_scanned_pdf_is_not_enough_evidence_not_an_error(no_ocr):
    r = await screen_candidate(
        jd_text=JD, filename="scanned_no_text.pdf",
        data=(PDFS / "scanned_no_text.pdf").read_bytes(), llm=LLMClient("mock"),
    )
    for key in BASE_KEYS:
        assert key in r, f"missing base contract key {key}"
    ext = r["extensions"]
    assert ext["status"] == "Not Enough Evidence" and "error" not in ext
    assert r["recommendation"] == "Review Manually"
    assert r["requirement_match"], "the JD yields requirements"
    for req in r["requirement_match"]:
        assert req["status"] == "Not Enough Evidence"
        assert "scan without a text layer" in req["notes"]
    assert not r["technical_gaps"] and not r["matched_skills"]  # never "Missing"
    assert ext["score_breakdown"]["score_confidence"] == 0
    assert r["overall_match_score"] == 0
    assert any("scan" in reason for reason in ext["recommendation_reasons"])
    assert ext["human_review_required"] is True
    assert ext["meta"]["stages"]["parse"]["status"] == "ok"
    assert ext["integrity"]["recommended_action"] == "proceed"  # a scan is never penalised
