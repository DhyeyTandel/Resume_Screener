"""Hostile uploads: every case is rejected fast and with bounded memory, and legitimate files
still load exactly as before."""
import asyncio
import sys
import time
import tracemalloc
import types
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.routes as routes
import app.parsing.loader as loader
from app import config
from app.main import BodyLimitMiddleware, app
from app.parsing.loader import NoTextLayer, UnreadableFile, load

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import make_hostile as H

FIX = Path(__file__).parent / "fixtures"
SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
MAX_SECONDS = 1.0
MAX_PEAK = 12 * 1024 * 1024  # a 15 MB bomb would peak at 15 MB or more if it were expanded


def measure(fn):
    """(result_or_exception, seconds, peak bytes allocated while running fn)."""
    tracemalloc.start()
    tracemalloc.reset_peak()
    t = time.perf_counter()
    try:
        out = fn()
    except Exception as exc:
        out = exc
    secs = time.perf_counter() - t
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return out, secs, peak


def rejected(name, data, why, max_peak=MAX_PEAK):
    out, secs, peak = measure(lambda: load(name, data))
    assert isinstance(out, UnreadableFile) and not isinstance(out, NoTextLayer), out
    assert why in out.reason, out.reason
    assert out.remediation and "Traceback" not in out.reason
    assert secs < MAX_SECONDS, secs
    assert peak < max_peak, peak
    print(f"{name} {why!r}: {secs * 1000:.0f} ms, peak {peak / 1024:.0f} KiB")


# --- DOCX / ZIP -----------------------------------------------------------------------------


def test_zip_bomb_high_ratio():
    data = H.zip_bomb()
    assert len(data) < 100_000
    rejected("bomb.docx", data, "compressed far more")


def test_oversized_member():
    rejected("big.docx", H.oversized_member(), "far larger than a resume")


def test_total_uncompressed_cap(monkeypatch):
    monkeypatch.setitem(config.get_config()["ingest"], "docx_max_ratio", 10**6)
    extra = {f"word/m{i}.bin": bytes(18 * 1024 * 1024) for i in range(3)}  # 54 MB in total
    rejected("total.docx", H.package(extra), "far more data")


def test_lying_header_cannot_bypass_the_check():
    # The header claims 100 bytes (under every cap); the real stream is 15 MB.
    rejected("lie.docx", H.lying_header(), "larger than its header says")


@pytest.mark.parametrize("n", [1000, 30000])  # second case is refused before zipfile lists it
def test_too_many_members(n):
    rejected("many.docx", H.many_members(n), "too many parts")


def test_deeply_nested_xml():
    rejected("deep.docx", H.deep_xml(), "deep or large")


def test_too_many_elements():
    # The parse is abandoned at the element cap, so memory is bounded by that cap (about 40 MB),
    # not by the size of the part.
    rejected("wide.docx", H.many_elements(), "deep or large", max_peak=64 * 1024 * 1024)


@pytest.mark.parametrize("name", ["../../evil.xml", "/etc/cron.d/x", "word/../../x", "C:/boot.ini"])
def test_path_traversal_member_name(name):
    rejected("trav.docx", H.traversal_member(name), "unsafe name")


def test_not_a_zip_and_truncated_zip_fail_cleanly():
    good = (FIX / "docx" / "clean.docx").read_bytes()
    for data in (b"not a zip at all", good[: len(good) // 2], b""):
        with pytest.raises(UnreadableFile):
            load("x.docx", data)


# --- PDF ------------------------------------------------------------------------------------


def test_pdf_too_many_pages():
    data = H.many_pages_pdf(2000)
    assert len(data) < 400_000
    rejected("pages.pdf", data, "pages")


def test_pdf_page_limit_boundary():
    assert load("ok.pdf", H.text_pdf(50)).pages == 50
    rejected("over.pdf", H.text_pdf(51), "51 pages")


def test_pdf_encrypted_and_malformed_fail_cleanly():
    with pytest.raises(UnreadableFile, match="password"):
        load("enc.pdf", H.encrypted_pdf())
    good = (FIX / "pdfs" / "clean.pdf").read_bytes()
    for data in (b"%PDF-1.7\ngarbage", good[:200], b"\x00" * 1000):
        with pytest.raises(UnreadableFile):
            load("bad.pdf", data)


def test_pdf_dense_text_is_capped(monkeypatch):
    monkeypatch.setitem(config.get_config()["ingest"], "pdf_max_spans", 3)
    with pytest.raises(UnreadableFile, match="more text"):
        load("dense.pdf", (FIX / "pdfs" / "clean.pdf").read_bytes())


def test_ocr_respects_page_limit(monkeypatch):
    calls = []
    tess = types.SimpleNamespace(
        get_tesseract_version=lambda: "5",
        image_to_string=lambda img: calls.append(1) or "text",
    )
    pil = types.ModuleType("PIL")
    pil.Image = types.SimpleNamespace(open=lambda b: object())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytesseract", tess)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    assert loader._ocr_pdf(H.text_pdf(30)) == "text\ntext\ntext\ntext\ntext"
    assert len(calls) == int(config.cfg("ingest.ocr_max_pages"))


# --- API ------------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    seen = {}

    async def fake_run(sid, jd, inputs, progress=None):
        seen["inputs"] = inputs

    monkeypatch.setattr(routes, "_run", fake_run)
    c = TestClient(app)
    c.seen = seen  # type: ignore[attr-defined]
    return c


JD = "Backend engineer wanted."


def test_too_many_files(client):
    files = [("files", (f"r{i}.txt", b"hello world", "text/plain")) for i in range(26)]
    t = time.perf_counter()
    r = client.post("/v1/screenings", data={"jd_text": JD}, files=files)
    assert r.status_code == 422 and r.json()["code"] == "TOO_MANY_FILES"
    assert r.json()["field"] == "files" and r.json()["remediation"]
    assert time.perf_counter() - t < MAX_SECONDS


def test_too_many_pasted_resumes(client):
    r = client.post("/v1/screenings", data={"jd_text": JD, "pasted_resumes": ["x"] * 26})
    assert r.status_code == 422 and r.json()["code"] == "TOO_MANY_PASTED_RESUMES"


def test_jd_and_pasted_text_length(client):
    r = client.post("/v1/screenings", data={"jd_text": "a" * 50001, "pasted_resumes": ["x"]})
    assert r.status_code == 413 and r.json()["code"] == "JD_TOO_LARGE"
    r = client.post("/v1/screenings", data={"jd_text": JD, "pasted_resumes": ["a" * 200001]})
    assert r.status_code == 413 and r.json()["code"] == "PASTED_RESUME_TOO_LARGE"


def test_linkedin_upload_cap(client):
    files = [
        ("files", ("a.txt", b"hello", "text/plain")),
        ("linkedin_files", ("li.json", b" " * (2 * 1024 * 1024 + 1), "application/json")),
    ]
    r = client.post("/v1/screenings", data={"jd_text": JD}, files=files)
    assert r.status_code == 413 and r.json()["code"] == "LINKEDIN_FILE_TOO_LARGE"


def test_oversized_multipart_by_content_length(monkeypatch, client):
    monkeypatch.setitem(config.get_config()["ingest"], "max_request_bytes", 100_000)
    files = [("files", ("a.txt", b"x" * 300_000, "text/plain"))]
    t = time.perf_counter()
    r = client.post("/v1/screenings", data={"jd_text": JD}, files=files)
    assert r.status_code == 413 and r.json()["code"] == "REQUEST_TOO_LARGE"
    assert time.perf_counter() - t < MAX_SECONDS
    assert "inputs" not in client.seen


def _asgi_post(receive, headers):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def inner(scope, rcv, snd):  # an app that tries to read the whole body
        while (await rcv()).get("more_body"):
            pass

    scope = {"type": "http", "method": "POST", "path": "/v1/screenings", "headers": headers}
    asyncio.run(BodyLimitMiddleware(inner)(scope, receive, send))
    return sent


def test_streamed_body_is_cut_off_at_the_limit_without_buffering():
    limit = int(config.cfg("ingest.max_request_bytes"))
    chunk, pulled = b"x" * 65536, []

    async def endless():  # never ends and sends no Content-Length (or lies about it)
        pulled.append(1)
        return {"type": "http.request", "body": chunk, "more_body": True}

    out, secs, peak = measure(lambda: _asgi_post(endless, [(b"content-length", b"10")]))
    status = [m for m in out if m["type"] == "http.response.start"][0]["status"]
    assert status == 413
    assert len(pulled) * len(chunk) <= limit + len(chunk)
    assert secs < MAX_SECONDS and peak < 2 * 1024 * 1024
    print(f"endless stream: {secs * 1000:.0f} ms, peak {peak / 1024:.0f} KiB, read {len(pulled) * 64} KiB")


def test_declared_oversize_is_refused_without_reading_any_body():
    async def never():
        raise AssertionError("body must not be read")

    out, secs, peak = measure(lambda: _asgi_post(never, [(b"content-length", b"9999999999")]))
    assert [m for m in out if m["type"] == "http.response.start"][0]["status"] == 413
    assert secs < MAX_SECONDS and peak < 1024 * 1024


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\evil.pdf", "evil.pdf"),
        ("a/b\\c.docx", "c.docx"),
        ("re\x00su\x07me\x1b[31m.pdf", "resume[31m.pdf"),
        ("name\u202etxt.exe.pdf", "nametxt.exe.pdf"),
        (".hidden.txt", "hidden.txt"),
        ("", "upload"),
        (None, "upload"),
        ("   \t\n", "upload"),
        ("priya_sharma.pdf", "priya_sharma.pdf"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert routes.sanitize_filename(raw) == expected


def test_sanitize_filename_bounds_length_and_keeps_extension():
    out = routes.sanitize_filename("a" * 5000 + ".docx")
    assert len(out) <= 120 and out.endswith(".docx")


def test_hostile_filename_is_sanitized_before_it_is_stored(client):
    nasty = "..\\..\\x/../Evil\x01\x1b_Name\u202e.txt"
    r = client.post(
        "/v1/screenings", data={"jd_text": JD},
        files=[("files", (nasty, b"hello", "text/plain"))],
    )
    assert r.status_code == 200
    item = client.seen["inputs"][0]
    for field in ("filename", "candidate_name"):
        assert not any(c in item[field] for c in "/\\\x01\x1b\u202e"), item[field]
    assert item["filename"].endswith(".txt")


def test_pasted_name_has_no_control_characters(client):
    r = client.post("/v1/screenings", data={"jd_text": JD, "pasted_resumes": ["Bob\x1b[2J\x07 Lee\nrest"]})
    assert r.status_code == 200
    name = client.seen["inputs"][0]["candidate_name"]
    assert "\x1b" not in name and "\x07" not in name


# --- legitimate files are unchanged ---------------------------------------------------------


class _PlainZip:
    """The loader's original zip access: plain zipfile reads, no caps."""

    def __init__(self, data):
        from io import BytesIO

        self.z = zipfile.ZipFile(BytesIO(data))

    def namelist(self):
        return self.z.namelist()

    def read(self, name):
        return self.z.read(name)


def _plain_load_xml(z, name):
    import xml.etree.ElementTree as ET

    raw = z.read(name)
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("dtd")
    return ET.fromstring(raw)


def _snapshot(doc):
    return (doc.visible_text, doc.raw_text_by_parser, doc.spans, doc.metadata, doc.pages,
            doc.page_size, doc.source, doc.ocr_used)


@pytest.mark.parametrize("path", sorted((FIX / "docx").glob("*.docx")), ids=lambda p: p.name)
def test_every_docx_fixture_loads_exactly_as_with_plain_zipfile(path, monkeypatch):
    data = path.read_bytes()
    new = _snapshot(load(path.name, data))
    with monkeypatch.context() as m:
        m.setattr(loader, "_SafeZip", _PlainZip)
        m.setattr(loader, "_load_xml", _plain_load_xml)
        old = _snapshot(load(path.name, data))
    assert new == old


@pytest.mark.parametrize("path", sorted((FIX / "pdfs").glob("*.pdf")), ids=lambda p: p.name)
def test_every_pdf_fixture_loads_exactly_as_without_limits(path, monkeypatch):
    def run():
        try:
            return _snapshot(load(path.name, path.read_bytes()))
        except UnreadableFile as exc:
            return (type(exc).__name__, exc.reason)

    new = run()
    ing = config.get_config()["ingest"]
    for k in ("pdf_max_pages", "pdf_max_spans", "pdf_max_chars"):
        monkeypatch.setitem(ing, k, 10**9)
    assert new == run()


@pytest.mark.parametrize("path", sorted((SAMPLES / "resumes").glob("*")), ids=lambda p: p.name)
def test_sample_resumes_load_as_text(path):
    text = path.read_text(encoding="utf-8")
    doc = load(path.name, path.read_bytes())
    assert doc.visible_text == text.strip() and doc.source == path.name
