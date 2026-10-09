"""LinkedIn "Save to PDF" upload (audit fix): a real PDF export is read with the sandboxed PDF
loader, not decoded as UTF-8. Spec 17 lists "malformed LinkedIn PDF"."""
import fitz  # PyMuPDF
import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.modules.authenticity_engine.collectors.linkedin import collect_linkedin

LINES = [
    "Priya Raman", "Backend Engineer", "Experience",
    "Northwind Payments", "Senior Backend Engineer", "January 2021 - Present (5 years)",
    "Corvid Systems", "Backend Engineer", "June 2019 - December 2020 (1 year 7 months)",
    "Education", "Ravenna University", "B.S. Computer Science, 2015 - 2019",
]


def linkedin_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for ln in LINES:
        page.insert_text((72, y), ln, fontsize=11)
        y += 20
    return doc.tobytes()


@pytest.fixture
def client(monkeypatch):
    seen = {}

    async def fake_run(sid, jd, inputs, progress=None):
        seen["inputs"] = inputs

    monkeypatch.setattr(routes, "_run", fake_run)
    c = TestClient(app)
    c.seen = seen  # type: ignore[attr-defined]
    return c


def post(client, name, data):
    return client.post(
        "/v1/screenings", data={"jd_text": "Backend engineer wanted."},
        files=[("files", ("a.txt", b"hello", "text/plain")),
               ("linkedin_files", (name, data, "application/pdf"))])


def test_real_linkedin_pdf_export_yields_its_roles(client):
    pdf = linkedin_pdf()
    assert pdf[:5] == b"%PDF-"
    assert post(client, "Profile.pdf", pdf).status_code in (200, 202)
    export = client.seen["inputs"][0]["linkedin_export"]
    assert export["type"] == "pdf_export"
    assert "Northwind Payments" in export["content"] and "\x00" not in export["content"]
    li = collect_linkedin(export)
    assert li.status == "ok"
    assert [(r.company, r.title, r.start, r.end) for r in li.roles] == [
        ("Northwind Payments", "Senior Backend Engineer", "2021", "Present"),
        ("Corvid Systems", "Backend Engineer", "2019", "2020"),
    ][: len(li.roles)]
    assert len(li.roles) >= 2


def test_pdf_detected_by_content_when_the_name_has_no_extension(client):
    assert post(client, "export", linkedin_pdf()).status_code in (200, 202)
    assert client.seen["inputs"][0]["linkedin_export"]["type"] == "pdf_export"
    assert "Northwind" in client.seen["inputs"][0]["linkedin_export"]["content"]


@pytest.mark.parametrize("name,data", [
    ("Profile.pdf", b"%PDF-1.7\nthis is not a real pdf \x00\xff\xfe garbage"),
    ("Profile.pdf", linkedin_pdf()[:300]),  # truncated
    ("Profile.pdf", b"plain text pretending to be a pdf"),
    ("Profile.pdf", b""),
])
def test_malformed_linkedin_pdf_is_a_clean_error_source_not_a_crash(client, name, data):
    r = post(client, name, data)
    assert r.status_code in (200, 202)
    export = client.seen["inputs"][0].get("linkedin_export")
    if export is None:  # an empty upload is simply not an export
        assert data == b""
        return
    li = collect_linkedin(export)
    assert li.status == "error" and li.roles == []
    assert li.error


def test_json_export_and_plain_text_export_still_work(client):
    r = client.post(
        "/v1/screenings", data={"jd_text": "Backend engineer wanted."},
        files=[("files", ("a.txt", b"hello", "text/plain")),
               ("linkedin_files", ("li.json", b'{"roles": [{"title": "T", "company": "C", "start": "2020", "end": "2021"}]}',
                                   "application/json"))])
    assert r.status_code in (200, 202)
    assert client.seen["inputs"][0]["linkedin_export"]["type"] == "structured_json"
    r = client.post(
        "/v1/screenings", data={"jd_text": "Backend engineer wanted."},
        files=[("files", ("a.txt", b"hello", "text/plain")),
               ("linkedin_files", ("li.txt", b"Acme\nEngineer\n2020 - Present", "text/plain"))])
    assert collect_linkedin(client.seen["inputs"][0]["linkedin_export"]).status == "ok"
