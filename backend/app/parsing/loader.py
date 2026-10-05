"""Stage 0 ingestion. PDF/DOCX/TXT + pasted text, with a dual-parser read.

PyMuPDF and pdfplumber are optional: when neither is installed the loader falls
back to a minimal built-in PDF text reader so the demo never dies (Spec 0.6).
"""
from __future__ import annotations

import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from io import BytesIO

from ..config import cfg

SUPPORTED = (".pdf", ".docx", ".txt", ".md")


class UnreadableFile(Exception):
    def __init__(self, reason: str, remediation: str) -> None:
        super().__init__(reason)
        self.reason, self.remediation = reason, remediation


class NoTextLayer(UnreadableFile):
    """A PDF that is an image only (a scan) and could not be OCR'd. Not a read failure: the
    pipeline reports it as "Not Enough Evidence" for manual review (Spec 7)."""


@dataclass
class Span:
    text: str
    size: float = 11.0
    color: int = 0x000000
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    render_mode: int = 0
    bg: int = 0xFFFFFF  # what the text is drawn on: page white unless highlighted or shaded
    origin: str = "body"  # "comment" for reviewer comments: quarantined, but not a hiding trick


@dataclass
class ParsedDoc:
    visible_text: str
    raw_text_by_parser: dict[str, str] = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    pages: int = 1
    page_size: tuple[float, float] = (612.0, 792.0)
    source: str = "text"
    ocr_used: bool = False


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
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_OFF = ("0", "false", "off")
_HEX6 = re.compile(r"[0-9A-Fa-f]{6}")
_WHITE = 0xFFFFFF
_HIDDEN_SIZE = 0.5  # what a run that is not rendered at all is recorded as

# Word's built-in highlight palette (w:highlight). "none" clears a highlight.
_HIGHLIGHT = {
    "black": 0x000000, "blue": 0x0000FF, "cyan": 0x00FFFF, "green": 0x00FF00,
    "magenta": 0xFF00FF, "red": 0xFF0000, "yellow": 0xFFFF00, "white": 0xFFFFFF,
    "darkBlue": 0x000080, "darkCyan": 0x008080, "darkGreen": 0x008000,
    "darkMagenta": 0x800080, "darkRed": 0x800000, "darkYellow": 0x808000,
    "darkGray": 0x808080, "lightGray": 0xC0C0C0,
}
# w:themeColor names -> theme colour-scheme slots (a:clrScheme children).
_THEME_SLOT = {
    "dark1": "dk1", "light1": "lt1", "dark2": "dk2", "light2": "lt2",
    "text1": "dk1", "background1": "lt1", "text2": "dk2", "background2": "lt2",
    "accent1": "accent1", "accent2": "accent2", "accent3": "accent3",
    "accent4": "accent4", "accent5": "accent5", "accent6": "accent6",
    "hyperlink": "hlink", "followedHyperlink": "folHlink",
}
# The Office theme, used when a document uses theme colours but ships no usable theme part.
_OFFICE_THEME = {
    "dk1": 0x000000, "lt1": 0xFFFFFF, "dk2": 0x44546A, "lt2": 0xE7E6E6,
    "accent1": 0x4472C4, "accent2": 0xED7D31, "accent3": 0xA5A5A5, "accent4": 0xFFC000,
    "accent5": 0x5B9BD5, "accent6": 0x70AD47, "hlink": 0x0563C1, "folHlink": 0x954F72,
}


def _wval(el) -> str | None:
    return None if el is None else el.get(_W + "val")


_DOCX_HELP = "Re-save the file from Word as a normal DOCX, or export it as a PDF, or paste the text."


def _lim(name: str, default: int) -> int:
    return int(cfg("ingest." + name, default))


def _bad_docx(why: str) -> UnreadableFile:
    return UnreadableFile(f"This DOCX was not opened because {why}.", _DOCX_HELP)


def _safe_member_name(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return not (
        not name or name.startswith(("/", "\\")) or "\x00" in name or ".." in parts
        or (len(name) > 1 and name[1] == ":")
    )


class _SafeZip:
    """A read-only view of a DOCX package that cannot be used to exhaust memory. The caps are
    checked on the declared sizes first, then again on the bytes that really come out of the
    decompressor, so a header that lies about its size gains nothing. Nothing is extracted to
    disk, but member names are still validated."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.max_member = _lim("docx_max_member_bytes", 20 * 1024 * 1024)
        self.budget = _lim("docx_max_total_bytes", 50 * 1024 * 1024)
        max_members = _lim("docx_max_members", 500)
        # The central directory size comes from the end record. Checking it before zipfile
        # builds one ZipInfo per entry keeps a package with millions of members cheap.
        eocd = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
        if eocd >= 0 and len(data) - eocd >= 22:
            cd_size = struct.unpack("<I", data[eocd + 12 : eocd + 16])[0]
            if cd_size > max_members * 1024:
                raise _bad_docx("it contains too many parts")
        zf = zipfile.ZipFile(BytesIO(data))
        infos = zf.infolist()
        if len(infos) > max_members:
            raise _bad_docx("it contains too many parts")
        total = 0
        ratio = _lim("docx_max_ratio", 100)
        floor = _lim("docx_ratio_floor_bytes", 65536)
        for i in infos:
            if not _safe_member_name(i.filename):
                raise _bad_docx("it contains a part with an unsafe name")
            if i.file_size > self.max_member:
                raise _bad_docx("one of its parts is far larger than a resume should be")
            if i.file_size > floor and i.file_size > ratio * max(i.compress_size, 1):
                raise _bad_docx("it is compressed far more than a real document would be")
            total += i.file_size
        if total > self.budget:
            raise _bad_docx("it would expand to far more data than a resume should hold")
        if total > floor and total > ratio * max(len(data), 1):
            raise _bad_docx("it is compressed far more than a real document would be")
        self.infos = {i.filename: i for i in infos}

    def namelist(self) -> list[str]:
        return list(self.infos)

    def read(self, name: str) -> bytes:
        """Decompress one member, counting the bytes actually produced."""
        info = self.infos[name]
        if info.flag_bits & 0x1:
            raise _bad_docx("it is password protected")
        off = info.header_offset
        hdr = self.data[off : off + 30]
        if len(hdr) < 30 or hdr[:4] != b"PK\x03\x04":
            raise _bad_docx("it is damaged")
        nlen, xlen = struct.unpack("<HH", hdr[26:30])
        start = off + 30 + nlen + xlen
        raw = self.data[start : start + info.compress_size]
        limit = min(info.file_size, self.max_member, self.budget)
        if info.compress_type == zipfile.ZIP_STORED:
            out = raw
            if len(out) > limit:
                raise _bad_docx("it contains a part that is larger than its header says")
        elif info.compress_type == zipfile.ZIP_DEFLATED:
            d = zlib.decompressobj(-15)
            parts: list[bytes] = []
            got, step = 0, 64 * 1024
            try:
                for pos in range(0, len(raw), step):
                    piece = d.decompress(raw[pos : pos + step], step)
                    while True:
                        got += len(piece)
                        if got > limit:  # the real output, not the header, is capped
                            raise _bad_docx("it contains a part that is larger than its header says")
                        parts.append(piece)
                        if d.eof or (not d.unconsumed_tail and len(piece) < step):
                            break  # a full-size piece may hide buffered output: ask again
                        piece = d.decompress(d.unconsumed_tail, step)
                    if d.eof:
                        break
            except zlib.error as exc:
                raise _bad_docx("it is damaged") from exc
            out = b"".join(parts)
        else:
            raise _bad_docx("it uses an unsupported compression method")
        if len(out) != info.file_size:
            raise _bad_docx("it is damaged")
        self.budget -= len(out)
        if self.budget < 0:
            raise _bad_docx("it would expand to far more data than a resume should hold")
        return out


def _load_xml(z, name: str):
    import xml.etree.ElementTree as ET

    raw = z.read(name)
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise ValueError(f"DTD or entity declarations are not allowed ({name})")
    # Depth and element count are counted while parsing, so a hostile part is abandoned
    # early instead of being built in full (and later walked by recursive code).
    max_depth, max_el = _lim("xml_max_depth", 100), _lim("xml_max_elements", 300000)
    parser: ET.XMLPullParser = ET.XMLPullParser(events=("start", "end"))
    depth = count = 0
    root = None
    for i in range(0, len(raw), 65536):
        parser.feed(raw[i : i + 65536])
        for event in parser.read_events():
            ev, el = event  # type: ignore[misc]  # typeshed types events as 1-tuples
            if ev == "start":
                root = el if root is None else root
                depth += 1
                count += 1
                if depth > max_depth or count > max_el:
                    raise _bad_docx("its text is structured in an unusually deep or large way")
            else:
                depth -= 1
    parser.close()
    if root is None:
        raise ET.ParseError("no element found")
    return root


def _theme_colors(root) -> dict[str, int]:
    """Theme colour scheme as {slot: rgb}; slots the file does not define come from Office."""
    out = dict(_OFFICE_THEME)
    if root is None:
        return out
    scheme = next(root.iter(_A + "clrScheme"), None)
    for slot in list(scheme) if scheme is not None else []:
        el = next(iter(slot), None)
        val = None if el is None else el.get("val") if el.tag == _A + "srgbClr" else el.get("lastClr")
        if val and _HEX6.fullmatch(val):
            out[slot.tag.removeprefix(_A)] = int(val, 16)
    return out


def _tint_shade(rgb: int, tint: str | None, shade: str | None) -> int:
    """w:themeTint lightens toward white, w:themeShade darkens toward black (per channel;
    Word works in HSL, so this is an approximation that is exact at the extremes)."""
    chans = [(rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255]
    if tint and _is_hex_byte(tint):
        t = int(tint, 16) / 255
        chans = [round(255 - (255 - c) * t) for c in chans]
    if shade and _is_hex_byte(shade):
        s = int(shade, 16) / 255
        chans = [round(c * s) for c in chans]
    return (chans[0] << 16) | (chans[1] << 8) | chans[2]


def _is_hex_byte(v: str) -> bool:
    return bool(re.fullmatch(r"[0-9A-Fa-f]{2}", v))


def _theme_rgb(theme: dict[str, int], name: str | None, tint=None, shade=None) -> int | None:
    slot = _THEME_SLOT.get(name or "")
    if slot is None or slot not in theme:
        return None
    return _tint_shade(theme[slot], tint, shade)


def _fill_rgb(el, theme: dict[str, int]) -> int | None:
    """Fill colour of a w:shd element (themeFill wins over fill); None = no fill."""
    if el is None:
        return None
    th = _theme_rgb(theme, el.get(_W + "themeFill"), el.get(_W + "themeFillTint"),
                    el.get(_W + "themeFillShade"))
    if th is not None:
        return th
    fill = el.get(_W + "fill")
    return int(fill, 16) if fill and _HEX6.fullmatch(fill) else None


def _rpr_props(rpr, theme: dict[str, int]) -> dict:
    """Run properties that matter for visibility: vanish, colour, size (pt), highlight and
    shading. A key that is absent means "inherit"; colour None means "automatic"."""
    out: dict = {}
    if rpr is None:
        return out
    v = rpr.find(_W + "vanish")
    if v is not None:
        out["vanish"] = (_wval(v) or "true").lower() not in _OFF
    c = rpr.find(_W + "color")
    if c is not None:
        val = c.get(_W + "val")
        literal = int(val, 16) if val and _HEX6.fullmatch(val) else None
        themed = _theme_rgb(theme, c.get(_W + "themeColor"), c.get(_W + "themeTint"),
                            c.get(_W + "themeShade"))
        # Word resolves w:themeColor and ignores w:val; LibreOffice renders w:val. When the
        # two disagree the run is invisible in one of them, so both are kept and the worse
        # contrast is judged.
        out["color"] = themed if themed is not None else literal
        out["color_alt"] = literal if themed is not None and literal != themed else None
    sz = _wval(rpr.find(_W + "sz"))
    if sz and sz.isdigit():
        out["size"] = int(sz) / 2  # w:sz is in half-points
    hl = rpr.find(_W + "highlight")
    if hl is not None:
        out["highlight"] = _HIGHLIGHT.get(_wval(hl) or "")
    shd = rpr.find(_W + "shd")
    if shd is not None:
        out["shd"] = _fill_rgb(shd, theme)
    return out


def _parse_styles(root, theme: dict[str, int]) -> dict:
    """docDefaults run props, styleId -> (basedOn, props), and the default style ids."""
    out: dict = {"defaults": {}, "table": {}, "default_p": None, "default_c": None}
    if root is None:
        return out
    dd = root.find(_W + "docDefaults/" + _W + "rPrDefault/" + _W + "rPr")
    out["defaults"] = _rpr_props(dd, theme)
    for st in root.iter(_W + "style"):
        sid = st.get(_W + "styleId")
        if not sid:
            continue
        props = _rpr_props(st.find(_W + "rPr"), theme)
        ppr = st.find(_W + "pPr")
        if ppr is not None and ppr.find(_W + "shd") is not None:
            props["pshd"] = _fill_rgb(ppr.find(_W + "shd"), theme)
        out["table"][sid] = (_wval(st.find(_W + "basedOn")), props)
        if st.get(_W + "default") in ("1", "true"):
            kind = st.get(_W + "type")
            if kind == "paragraph":
                out["default_p"] = sid
            elif kind == "character":
                out["default_c"] = sid
    return out


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


def _para_runs(el):
    """Runs that belong to this paragraph. Text boxes are not descended into (their
    paragraphs are yielded by _paras on their own, so nothing is counted twice) and only
    the mc:Choice branch of an AlternateContent is read."""
    for ch in el:
        if ch.tag == _W + "r":
            yield ch
        elif ch.tag == _MC + "AlternateContent":
            choice = ch.find(_MC + "Choice")
            if choice is not None:
                yield from _para_runs(choice)
        elif ch.tag not in (_W + "pPr", _W + "txbxContent", _MC + "Fallback"):
            yield from _para_runs(ch)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _paras(el):
    """Yields (paragraph, unrendered_copy). Paragraphs inside text boxes and shapes
    (w:txbxContent) are included. Word writes a text box twice, as DrawingML in mc:Choice
    and as VML in mc:Fallback. Only the Choice is rendered; a Fallback whose text is the same
    is skipped as a duplicate, and one that says something different is returned flagged
    as unrendered (a different story told to parsers that read the Fallback)."""
    for ch in el:
        if ch.tag == _MC + "AlternateContent":
            choice, fb = ch.find(_MC + "Choice"), ch.find(_MC + "Fallback")
            chosen = list(_paras(choice)) if choice is not None else []
            yield from chosen
            if fb is not None:
                fb_paras = [p for p, _ in _paras(fb)]
                if _sig(fb_paras) != _sig([p for p, _ in chosen]):
                    for p in fb_paras:
                        yield p, True
        elif ch.tag == _W + "p":
            yield ch, False
            yield from _paras(ch)  # text boxes anchored in this paragraph's runs
        else:
            yield from _paras(ch)


def _sig(paras) -> list[str]:
    sigs = (_norm("".join(_run_text(r) for r in _para_runs(p))) for p in paras)
    return [s for s in sigs if s]


class _Docx:
    """Reads one package; turns each paragraph into spans and visible lines."""

    def __init__(self, styles: dict, theme: dict[str, int]) -> None:
        self.styles, self.theme = styles, theme
        self.spans: list[Span] = []

    def part(self, root, *, hidden: bool = False, origin: str = "body") -> list[str]:
        """Visible lines of one XML part. hidden=True records every run as unrendered."""
        from ..modules.integrity_guard.scanner import contrast_ratio, is_hidden

        parent = {c: p for p in root.iter() for c in p}
        lines: list[str] = []
        for para, unrendered in _paras(root):
            force = hidden or unrendered
            ppr = para.find(_W + "pPr")
            sid = _wval(ppr.find(_W + "pStyle")) if ppr is not None else None
            table = self.styles["table"]
            para_style = _resolve_style(table, sid or self.styles["default_p"])
            para_shd = para_style.get("pshd")
            if ppr is not None and ppr.find(_W + "shd") is not None:
                para_shd = _fill_rgb(ppr.find(_W + "shd"), self.theme)
            cell = self._cell_shading(para, parent)
            visible_runs: list[str] = []
            for run in _para_runs(para):
                text = _run_text(run)
                if not text.strip():
                    if text:
                        visible_runs.append(text)  # keep spacing between runs
                    continue
                rpr = run.find(_W + "rPr")
                # Precedence: docDefaults < paragraph style < character style < direct.
                props = dict(self.styles["defaults"])
                props.update(para_style)
                props.update(_resolve_style(table, self.styles["default_c"]))
                props.update(_resolve_style(table, _wval(rpr.find(_W + "rStyle")) if rpr is not None else None))
                props.update(_rpr_props(rpr, self.theme))
                bg = next(
                    (c for c in (props.get("highlight"), props.get("shd"), para_shd, cell)
                     if c is not None),
                    _WHITE,
                )
                size = float(props.get("size", 11.0))
                if force or props.get("vanish"):
                    size = _HIDDEN_SIZE
                span = Span(text=text, size=size, color=_text_colour(props, bg, contrast_ratio), bg=bg,
                            origin=origin)
                self.spans.append(span)
                if not is_hidden(span, (612.0, 792.0)):
                    visible_runs.append(text)
            line = "".join(visible_runs).strip()
            if line:
                lines.append(line)
        return lines

    def _cell_shading(self, para, parent) -> int | None:
        node = parent.get(para)
        while node is not None:
            if node.tag == _W + "tc":
                tcpr = node.find(_W + "tcPr")
                shd = tcpr.find(_W + "shd") if tcpr is not None else None
                return _fill_rgb(shd, self.theme)
            node = parent.get(node)
        return None


def _text_colour(props: dict, bg: int, contrast_ratio) -> int:
    """The colour to judge. Automatic colour is black on light and white on dark shading
    (as Word draws it). With two candidate colours the less readable one is judged."""
    cands = [props.get("color"), props.get("color_alt")]
    real = [c for c in cands if c is not None]
    if not real:
        return 0x000000 if _luminance_of(bg) > 0.18 else _WHITE
    return min(real, key=lambda c: contrast_ratio(c, bg))


def _luminance_of(rgb: int) -> float:
    from ..modules.integrity_guard.scanner import _luminance

    return _luminance(rgb)


def _rels(z: _SafeZip, names: set[str]) -> dict[str, tuple[str, str]]:
    """document.xml.rels as {rId: (relationship type suffix, package path)}."""
    import posixpath

    out: dict[str, tuple[str, str]] = {}
    if "word/_rels/document.xml.rels" not in names:
        return out
    try:
        root = _load_xml(z, "word/_rels/document.xml.rels")
    except (ValueError, UnreadableFile):
        raise  # DTD / entity declarations and hostile parts are rejected outright
    except Exception:
        return out
    for rel in root:
        if rel.get("TargetMode") == "External" or not rel.get("Id"):
            continue
        target = rel.get("Target") or ""
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("word/" + target)
        out[rel.get("Id")] = ((rel.get("Type") or "").rsplit("/", 1)[-1], path)
    return out


_HIDDEN_PART = re.compile(r"word/((header|footer|footnotes|endnotes|comments)[^/]*|glossary/.+)\.xml$")


def _docx(data: bytes, filename: str) -> ParsedDoc:
    import html

    try:
        z = _SafeZip(data)
        names = set(z.namelist())
        root = _load_xml(z, "word/document.xml")
        core = z.read("docProps/core.xml").decode("utf-8", "replace") if "docProps/core.xml" in names else ""
        rels = _rels(z, names)

        def optional(path: str | None):
            if not path or path not in names:
                return None
            try:
                return _load_xml(z, path)
            except (ValueError, UnreadableFile):
                raise
            except Exception:
                return None  # an unparseable optional part adds no text a reader could see

        theme_path = next((p for t, p in rels.values() if t == "theme"), "word/theme/theme1.xml")
        theme = _theme_colors(optional(theme_path))
        styles_path = next((p for t, p in rels.values() if t == "styles"), "word/styles.xml")
        styles = _parse_styles(optional(styles_path), theme)
        settings = optional(next((p for t, p in rels.values() if t == "settings"), "word/settings.xml"))
        even_odd = settings is not None and any(
            (_wval(e) or "true").lower() not in _OFF for e in settings.iter(_W + "evenAndOddHeaders")
        )

        docx = _Docx(styles, theme)
        body = docx.part(root)

        # Headers and footers print on every page, so their text counts as resume text,
        # but only the ones a section really displays: a first-page header needs
        # w:titlePg and an even-page one needs evenAndOddHeaders.
        shown: dict[str, list[str]] = {"header": [], "footer": []}
        for sect in root.iter(_W + "sectPr"):
            title_pg = sect.find(_W + "titlePg")
            first_on = title_pg is not None and (_wval(title_pg) or "true").lower() not in _OFF
            for ref in list(sect):
                kind = ref.tag.removeprefix(_W).removesuffix("Reference")
                if ref.tag not in (_W + "headerReference", _W + "footerReference"):
                    continue
                typ = ref.get(_W + "type", "default")
                on = typ == "default" or (typ == "first" and first_on) or (typ == "even" and even_odd)
                rid = ref.get(_R + "id")
                if on and rid in rels and rels[rid][1] not in shown[kind]:
                    shown[kind].append(rels[rid][1])
        done: set[str] = set()
        seen_hf: set[str] = set()
        hf_lines: dict[str, list[str]] = {"header": [], "footer": []}
        for kind in ("header", "footer"):
            for path in shown[kind]:
                part_root = optional(path)
                done.add(path)
                if part_root is not None:
                    for line in docx.part(part_root):
                        if line not in seen_hf:
                            seen_hf.add(line)
                            hf_lines[kind].append(line)

        # Footnotes and endnotes: visible only when the body references them.
        note_lines: list[str] = []
        for kind in ("footnote", "endnote"):
            path = next((p for t, p in rels.values() if t == kind + "s"), None)  # type: ignore[assignment]  # reuses the name `path` from the loop above
            part_root = optional(path)
            if part_root is None:
                continue
            done.add(path)
            used = {e.get(_W + "id") for e in root.iter(_W + kind + "Reference")}
            by_id = {n.get(_W + "id"): n for n in part_root.iter(_W + kind)}
            for nid, node in by_id.items():
                note_lines += docx.part(node, hidden=nid not in used)

        # Everything else that carries text but never prints: comments (not part of the
        # printed page), and header/footer/note parts nothing displays.
        for name in sorted(names - done):
            if _HIDDEN_PART.fullmatch(name):
                part_root = optional(name)
                if part_root is not None:
                    origin = "comment" if "/comments" in name else "body"
                    docx.part(part_root, hidden=True, origin=origin)
    except UnreadableFile:
        raise
    except Exception as exc:
        raise UnreadableFile(
            f"The DOCX file could not be opened ({exc}).", "Re-export as PDF or paste the text."
        ) from exc

    meta = {
        k: html.unescape(v)
        for k, v in re.findall(r"<(?:dc|cp):(\w+)(?:\s[^>]*)?>([^<]*)</(?:dc|cp):\w+>", core)
    }
    text = "\n".join(hf_lines["header"] + body + note_lines + hf_lines["footer"])
    return ParsedDoc(
        visible_text=text,
        # One entry only: the hidden runs are not a second parser's view, and a second
        # entry would make the scanner report spurious PARSER_DIVERGENCE.
        raw_text_by_parser={"docx": text},
        spans=docx.spans,
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
            import fitz  # type: ignore[no-redef]  # older PyMuPDF

        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise UnreadableFile(
                "The PDF is password protected.", "Remove the password or paste the text."
            )
        pages = doc.page_count
        _check_pdf_pages(pages)
        meta = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
        text_parts = []
        max_spans, max_chars = _lim("pdf_max_spans", 200000), _lim("pdf_max_chars", 2000000)
        chars = 0
        for page in doc:  # type: ignore[attr-defined]  # pymupdf stubs omit Document.__iter__
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
                        chars += len(sp["text"])
                        if len(spans) >= max_spans or chars > max_chars:
                            raise _pdf_too_dense()
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
            _check_pdf_pages(pages)
            by_parser["pdfplumber"] = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except ImportError:
        pass
    except UnreadableFile:
        raise
    except Exception:
        by_parser["pdfplumber"] = ""

    if not by_parser:
        by_parser["builtin"] = _builtin_pdf_text(data)
        spans = [Span(text=t) for t in by_parser["builtin"].splitlines() if t.strip()]
    ocr_used = False
    if not any(by_parser.values()):
        ocr_text = _ocr_pdf(data)
        if not ocr_text:
            raise NoTextLayer(
                "No text layer was found in this PDF.",
                "This may be a scan - enable OCR or paste the text.",
            )
        # Recognised text is what a reader sees, so it is visible and has no hidden-text
        # signal of its own: it is one reader's view, not a second parser.
        by_parser = {"ocr": ocr_text}
        spans = [Span(text=t) for t in ocr_text.splitlines() if t.strip()]
        ocr_used = True
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
        ocr_used=ocr_used,
    )


def _check_pdf_pages(pages: int) -> None:
    limit = _lim("pdf_max_pages", 50)
    if pages > limit:
        raise UnreadableFile(
            f"This PDF has {pages} pages; the limit is {limit}.",
            "Upload just the resume pages, or paste the text.",
        )


def _pdf_too_dense() -> UnreadableFile:
    return UnreadableFile(
        "This PDF holds far more text than a resume should.",
        "Upload just the resume pages, or paste the text.",
    )


def _ocr_pdf(data: bytes) -> str | None:
    """OCR for a PDF with no text layer. Optional (Spec 7): needs the pytesseract package,
    Pillow and a tesseract binary. Returns None when any of them is missing, when OCR fails,
    or when it finds no text, so the caller falls back to NoTextLayer."""
    try:
        import pytesseract
        from PIL import Image

        pytesseract.get_tesseract_version()  # raises if the binary is not installed
    except Exception:
        return None
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz  # type: ignore[no-redef]

        doc = fitz.open(stream=data, filetype="pdf")
        limit = min(int(cfg("ingest.ocr_max_pages", 5)), _lim("pdf_max_pages", 50))
        out = []
        for n in range(min(limit, doc.page_count)):  # never materialise every page
            page = doc.load_page(n)
            pix = page.get_pixmap(dpi=200)
            out.append(pytesseract.image_to_string(Image.open(BytesIO(pix.tobytes("png")))))
        return "\n".join(out).strip() or None
    except Exception:
        return None


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
