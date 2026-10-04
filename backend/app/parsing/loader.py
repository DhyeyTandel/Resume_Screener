"""Stage 0 ingestion. PDF/DOCX/TXT + pasted text, with a dual-parser read.

PyMuPDF and pdfplumber are optional: when neither is installed the loader falls
back to a minimal built-in PDF text reader so the demo never dies (Spec 0.6).
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from ..config import cfg

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
    max_bytes = int(cfg("ingest.max_bytes", 10 * 1024 * 1024))
    if len(data) > max_bytes:
        mb = max_bytes / (1024 * 1024)
        raise UnreadableFile(
            f"File is larger than the {mb:g} MB upload limit.",
            "Compress or re-export the file under the limit, or paste the text instead.",
        )
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


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_OFF = ("0", "false", "off")


def _wval(el) -> str | None:
    return None if el is None else el.get(_W + "val")


def _rpr_props(rpr) -> dict:
    """Run properties that matter for visibility: vanish, colour, size (pt)."""
    out: dict = {}
    if rpr is None:
        return out
    v = rpr.find(_W + "vanish")
    if v is not None:
        out["vanish"] = (_wval(v) or "true").lower() not in _OFF
    c = _wval(rpr.find(_W + "color"))
    if c is not None:
        out["color"] = int(c, 16) if re.fullmatch(r"[0-9A-Fa-f]{6}", c) else 0x000000
    sz = _wval(rpr.find(_W + "sz"))
    if sz and sz.isdigit():
        out["size"] = int(sz) / 2  # w:sz is in half-points
    return out


def _style_table(styles_xml: str) -> dict[str, tuple[str | None, dict]]:
    """styleId -> (basedOn, props). Only the visibility-related props are kept."""
    import xml.etree.ElementTree as ET

    table: dict[str, tuple[str | None, dict]] = {}
    try:
        root = ET.fromstring(styles_xml)
    except ET.ParseError:
        return table
    for st in root.iter(_W + "style"):
        sid = st.get(_W + "styleId")
        if sid:
            table[sid] = (_wval(st.find(_W + "basedOn")), _rpr_props(st.find(_W + "rPr")))
    return table


def _resolve_style(table: dict, sid: str | None) -> dict:
    """Props of a style merged down its basedOn chain (child wins)."""
    chain: list[dict] = []
    seen: set[str] = set()
    while sid and sid in table and sid not in seen:
        seen.add(sid)
        based, props = table[sid]
        chain.append(props)
        sid = based
    merged: dict = {}
    for props in reversed(chain):
        merged.update(props)
    return merged


def _run_text(run) -> str:
    parts: list[str] = []
    for el in run:
        if el.tag == _W + "t":
            parts.append(el.text or "")
        elif el.tag == _W + "tab":
            parts.append("\t")
        elif el.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _docx(data: bytes, filename: str) -> ParsedDoc:
    import html
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            names = z.namelist()
            raw = z.read("word/document.xml")
            if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("DTD or entity declarations are not allowed")
            root = ET.fromstring(raw)
            core = z.read("docProps/core.xml").decode("utf-8", "replace") if "docProps/core.xml" in names else ""
            styles_xml = z.read("word/styles.xml").decode("utf-8", "replace") if "word/styles.xml" in names else ""
    except Exception as exc:
        raise UnreadableFile(
            f"The DOCX file could not be opened ({exc}).", "Re-export as PDF or paste the text."
        ) from exc
    styles = _style_table(styles_xml) if styles_xml else {}

    from ..modules.integrity_guard.scanner import is_hidden

    spans: list[Span] = []
    visible_paras: list[str] = []
    for para in root.iter(_W + "p"):
        ppr = para.find(_W + "pPr")
        para_style = _resolve_style(styles, _wval(ppr.find(_W + "pStyle")) if ppr is not None else None)
        visible_runs: list[str] = []
        for run in para.iter(_W + "r"):
            text = _run_text(run)
            if not text.strip():
                if text:
                    visible_runs.append(text)  # keep spacing between runs
                continue
            rpr = run.find(_W + "rPr")
            # Precedence: paragraph style < character style < direct formatting.
            props = dict(para_style)
            props.update(_resolve_style(styles, _wval(rpr.find(_W + "rStyle")) if rpr is not None else None))
            props.update(_rpr_props(rpr))
            hidden = bool(props.get("vanish"))
            size = float(props.get("size", 11.0))
            span = Span(text=text, size=0.5 if hidden else size, color=int(props.get("color", 0)))
            spans.append(span)
            if not is_hidden(span, (612.0, 792.0)):
                visible_runs.append(text)
        line = "".join(visible_runs).strip()
        if line:
            visible_paras.append(line)
    meta = {
        k: html.unescape(v)
        for k, v in re.findall(r"<(?:dc|cp):(\w+)(?:\s[^>]*)?>([^<]*)</(?:dc|cp):\w+>", core)
    }
    body = "\n".join(visible_paras)
    return ParsedDoc(
        visible_text=body,
        # One entry only: the hidden runs are not a second parser's view, and a second
        # entry would make the scanner report spurious PARSER_DIVERGENCE.
        raw_text_by_parser={"docx": body},
        spans=spans,
        metadata=meta,
        source=filename,
    )


def _pdf(data: bytes, filename: str) -> ParsedDoc:
    spans: list[Span] = []
    by_parser: dict[str, str] = {}
    pages, page_size, meta = 1, (612.0, 792.0), {}
    try:
        try:
            import pymupdf as fitz  # PyMuPDF >= 1.24.3
        except ImportError:
            import fitz  # older PyMuPDF

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
            # Span dicts carry no render mode, so take invisible (mode 3) runs
            # from the text trace and match spans to them by position.
            invisible = _invisible_boxes(page)
            # Keep text outside the page box; the default clip silently drops it
            # (a flag alone is not enough, an explicit infinite clip is), which
            # would hide off-page stuffing from the scanner.
            everything = fitz.INFINITE_RECT()
            for block in page.get_text("dict", clip=everything)["blocks"]:
                for line in block.get("lines", []):
                    for sp in line.get("spans", []):
                        bbox = tuple(sp["bbox"])
                        spans.append(
                            Span(
                                text=sp["text"],
                                size=float(sp["size"]),
                                color=int(sp.get("color", 0)),
                                bbox=bbox,
                                render_mode=int(
                                    sp.get("render_mode", 3 if _in_boxes(bbox, invisible) else 0)
                                ),
                            )
                        )
            text_parts.append(page.get_text("text", clip=everything))
        by_parser["pymupdf"] = "\n".join(text_parts)
    except UnreadableFile:
        raise
    except ImportError:
        pass
    except Exception as exc:
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
    except Exception:
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


def _invisible_boxes(page) -> list[tuple[float, float, float, float]]:
    try:
        return [tuple(t["bbox"]) for t in page.get_texttrace() if t.get("type") == 3]
    except Exception:
        return []


def _in_boxes(bbox, boxes) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)


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
