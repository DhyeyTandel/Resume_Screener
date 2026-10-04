"""Module A against real PDF files built by tests/fixtures/make_pdfs.py."""
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")

from app.modules.integrity_guard.scanner import scan  # noqa: E402
from app.parsing.loader import load  # noqa: E402

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
