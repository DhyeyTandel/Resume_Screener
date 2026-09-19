"""Stage 0 ingestion. PDF/DOCX/TXT + pasted text, with a dual-parser read.

PyMuPDF and pdfplumber are optional: when neither is installed the loader falls
back to a minimal built-in PDF text reader so the demo never dies (Spec 0.6).
"""
from __future__ import annotations
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

MAX_BYTES = 10 * 1024 * 1024
SUPPORTED = (".pdf", ".docx", ".txt", ".md")


class UnreadableFile(Exception):
    def __init__(self, reason: str, remediation: str) -> None:
        super().__init__(reason)
        self.reason, self.remediation = reason, remediation


@dataclass
class Span:
    text: str
    size: float = 11.0
    color: int = 0x000000
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    render_mode: int = 0


@dataclass
class ParsedDoc:
    visible_text: str
    raw_text_by_parser: dict[str, str] = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    pages: int = 1
    page_size: tuple[float, float] = (612.0, 792.0)
    source: str = "text"


def load(filename: str, data: bytes) -> ParsedDoc:
    if len(data) > MAX_BYTES:
        raise UnreadableFile("File is larger than 10 MB.", "Compress the file or paste the text.")
    low = filename.lower()
    if not low.endswith(SUPPORTED):
        raise UnreadableFile(
            f"Unsupported file type: {filename}.", "Upload a PDF, DOCX or TXT file."
        )
    if low.endswith((".txt", ".md")):
        return from_text(data.decode("utf-8", "replace"), source=filename)
    if low.endswith(".docx"):
        return _docx(data, filename)
    return _pdf(data, filename)


def from_text(text: str, source: str = "pasted") -> ParsedDoc:
    return ParsedDoc(
        visible_text=text.strip(),
        raw_text_by_parser={"text": text.strip()},
        spans=[Span(text=line) for line in text.splitlines() if line.strip()],
        source=source,
    )


def _docx(data: bytes, filename: str) -> ParsedDoc:
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
            core = ""
            if "docProps/core.xml" in z.namelist():
                core = z.read("docProps/core.xml").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise UnreadableFile(
            f"The DOCX file could not be opened ({exc}).", "Re-export as PDF or paste the text."
        ) from exc
    spans: list[Span] = []
    visible: list[str] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S) or re.findall(r"<w:p\b.*?</w:p>", xml, re.S):
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S))
        text = re.sub(r"<[^>]+>", "", text).strip()
        if not text:
            continue
        hidden = "<w:vanish" in para
        m = re.search(r'<w:color w:val="([0-9A-Fa-f]{6})"', para)
        color = int(m.group(1), 16) if m else 0x000000
        sz = re.search(r'<w:sz w:val="(\d+)"', para)
        size = int(sz.group(1)) / 2 if sz else 11.0
        spans.append(Span(text=text, size=0.5 if hidden else size, color=color))
        if not hidden and color != 0xFFFFFF and size > 4.0:
            visible.append(text)
    meta = {
        k: v
        for k, v in re.findall(r"<(?:dc|cp):(\w+)>([^<]*)</(?:dc|cp):\w+>", core)
    }
    body = "\n".join(visible)
    return ParsedDoc(
        visible_text=body,
        raw_text_by_parser={"docx": body, "docx_all": "\n".join(s.text for s in spans)},
        spans=spans,
        metadata=meta,
        source=filename,
    )


def _pdf(data: bytes, filename: str) -> ParsedDoc:
    spans: list[Span] = []
    by_parser: dict[str, str] = {}
    pages, page_size, meta = 1, (612.0, 792.0), {}
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise UnreadableFile(
                "The PDF is password protected.", "Remove the password or paste the text."
            )
        pages = doc.page_count
        meta = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
        text_parts = []
        for page in doc:
            page_size = (page.rect.width, page.rect.height)
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for sp in line.get("spans", []):
                        spans.append(
                            Span(
                                text=sp["text"],
                                size=float(sp["size"]),
                                color=int(sp.get("color", 0)),
                                bbox=tuple(sp["bbox"]),
                                render_mode=int(sp.get("render_mode", 0)),
                            )
                        )
            text_parts.append(page.get_text())
        by_parser["pymupdf"] = "\n".join(text_parts)
    except UnreadableFile:
        raise
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        raise UnreadableFile(
            f"The PDF could not be read ({exc}).", "Re-export as PDF or paste the text."
        ) from exc

    try:
        import pdfplumber

        with pdfplumber.open(BytesIO(data)) as pdf:
            pages = len(pdf.pages)
            by_parser["pdfplumber"] = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - second parser is best-effort
        by_parser["pdfplumber"] = ""

    if not by_parser:
        by_parser["builtin"] = _builtin_pdf_text(data)
        spans = [Span(text=t) for t in by_parser["builtin"].splitlines() if t.strip()]
    if not any(by_parser.values()):
        raise UnreadableFile(
            "No text layer was found in this PDF.",
            "This may be a scan - enable OCR or paste the text.",
        )
    primary = by_parser.get("pymupdf") or next(iter(by_parser.values()))
    visible = "\n".join(
        s.text for s in spans if _is_visible(s, page_size)
    ).strip() or primary.strip()
    return ParsedDoc(
        visible_text=visible,
        raw_text_by_parser=by_parser,
        spans=spans,
        metadata=meta,
        pages=pages,
        page_size=page_size,
        source=filename,
    )


def _is_visible(span: Span, page_size: tuple[float, float]) -> bool:
    from ..modules.integrity_guard.scanner import is_hidden

    return not is_hidden(span, page_size)


def _builtin_pdf_text(data: bytes) -> str:
    """Last-resort extractor for uncompressed/Flate text streams."""
    import zlib

    out: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        chunk = m.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            pass
        for t in re.findall(rb"\((?:\\.|[^\\()])*\)", chunk):
            s = t[1:-1].replace(rb"\(", b"(").replace(rb"\)", b")")
            out.append(s.decode("latin-1", "replace"))
    return "\n".join(out)
