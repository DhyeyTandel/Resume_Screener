"""Module A against real DOCX files built by tests/fixtures/make_docx.py."""
import zipfile
from pathlib import Path

import pytest

from app.llm.client import LLMClient
from app.modules.integrity_guard.scanner import scan
from app.parsing.loader import UnreadableFile, load
from app.pipeline.orchestrator import screen_candidate

DOCX = Path(__file__).parent / "fixtures" / "docx"
JD = (Path(__file__).resolve().parents[2] / "sample_data" / "jd_backend_engineer.txt").read_text()


def run(name: str):
    doc = load(name, (DOCX / name).read_bytes())
    return doc, scan(doc, JD)


def codes(report) -> set[str]:
    return {f["code"] for f in report["flags"]}


def test_fixtures_are_valid_packages():
    for p in DOCX.glob("*.docx"):
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "docProps/core.xml"} <= names


def test_clean():
    doc, r = run("clean.docx")
    assert r["verdict"] == "clean" and r["penalty"] == 1.0
    assert r["flags"] == []
    assert "FastAPI" in doc.visible_text and "Priya Sharma" in doc.visible_text


@pytest.mark.parametrize("name", ["vanish.docx", "white_text.docx", "tiny_font.docx", "mixed_run.docx"])
def test_hidden_text_variants(name):
    doc, r = run(name)
    assert codes(r) == {"HIDDEN_TEXT"}
    assert r["verdict"] == "suspicious" and r["penalty"] == 0.75
    assert "Kubernetes" not in doc.visible_text and "Terraform" not in doc.visible_text
    assert "Kubernetes" in r["hidden_text"]
    assert "FastAPI" in doc.visible_text


def test_injection_hidden():
    doc, r = run("injection_hidden.docx")
    assert {"HIDDEN_TEXT", "INJECTION_HIDDEN"} <= codes(r)
    assert r["verdict"] == "attack" and r["penalty"] == 0.40
    assert "ignore all previous" not in doc.visible_text.lower()
    assert "10/10" not in doc.visible_text
    assert "ignore all previous" in r["hidden_text"].lower()


def test_jd_clone_hidden():
    doc, r = run("jd_clone_hidden.docx")
    assert {"HIDDEN_TEXT", "JD_CLONE"} <= codes(r)
    assert r["verdict"] == "attack" and r["penalty"] == 0.40
    assert "FastAPI" in doc.visible_text
    assert "About the role" not in doc.visible_text and "pytest" not in doc.visible_text


def test_metadata_stuffed():
    doc, r = run("metadata_stuffed.docx")
    assert codes(r) == {"METADATA_STUFF"}
    assert r["verdict"] == "suspicious" and r["penalty"] == 0.75
    assert "kafka" in doc.metadata["keywords"]


def test_mixed_run_keeps_visible_run_and_drops_hidden_run():
    doc, r = run("mixed_run.docx")
    last = doc.visible_text.splitlines()[-1]
    assert last == "B.Tech Computer Science, State University, 2022"
    assert "Kubernetes" not in doc.visible_text
    assert "Kubernetes Terraform Kafka expert" in r["hidden_text"]
    assert "B.Tech" not in r["hidden_text"]


def test_style_based_hiding_is_resolved():
    # vanish via character style, white via a basedOn chain, 1pt via paragraph style.
    doc, r = run("style_hidden.docx")
    assert {"HIDDEN_TEXT", "INJECTION_HIDDEN"} <= codes(r)
    assert r["verdict"] == "attack"
    for leaked in ("Kubernetes", "Ignore all", "Redis"):
        assert leaked not in doc.visible_text
        assert leaked in r["hidden_text"]


def test_no_parser_divergence_on_any_docx():
    for p in DOCX.glob("*.docx"):
        assert "PARSER_DIVERGENCE" not in codes(run(p.name)[1]), p.name


async def test_end_to_end_injection_never_reaches_non_integrity_prompts():
    llm = LLMClient("mock")
    name = "injection_hidden.docx"
    result = await screen_candidate(
        jd_text=JD, filename=name, data=(DOCX / name).read_bytes(), llm=llm
    )
    assert result["extensions"]["integrity"]["recommended_action"] == "disqualify_review"
    others = [p for p in llm.prompts if p["task"] != "integrity_interpret"]
    assert others, "expected downstream LLM prompts"
    for p in others:
        blob = (p["system"] + p["user"]).lower()
        assert "ignore all previous" not in blob and "10/10" not in blob


def test_oversized_file_is_rejected_with_remediation(monkeypatch):
    from app import config

    monkeypatch.setitem(config.get_config()["ingest"], "max_bytes", 100)
    with pytest.raises(UnreadableFile) as e:
        load("resume.docx", b"x" * 101)
    assert "limit" in e.value.reason
    assert "paste the text" in e.value.remediation
    # Under the limit it gets past the size check (and fails later as a bad zip).
    with pytest.raises(UnreadableFile) as e2:
        load("resume.docx", b"x" * 50)
    assert "could not be opened" in e2.value.reason


def test_default_limit_comes_from_config():
    from app.config import cfg

    assert cfg("ingest.max_bytes") == 10 * 1024 * 1024
    with pytest.raises(UnreadableFile):
        load("big.txt", b"a" * (10 * 1024 * 1024 + 1))
