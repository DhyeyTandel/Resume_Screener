"""Builds hostile uploads in memory for test_upload_hardening.py. Nothing is written to disk and
nothing large is committed: the bombs are a few KB of compressed zeros until a parser opens them."""
from __future__ import annotations

import struct
import zipfile
from io import BytesIO

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
BASE = {
    "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
    "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
}


def doc_xml(body: str = "<w:p><w:r><w:t>Hello</w:t></w:r></w:p>") -> str:
    return f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>'


def package(extra: dict[str, bytes | str] | None = None, document: str | bytes | None = None,
            level: int = 9) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as z:
        for name, text in BASE.items():
            z.writestr(name, text)
        z.writestr("word/document.xml", document if document is not None else doc_xml())
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def zip_bomb(mb: int = 15) -> bytes:
    """A few KB that unpack to `mb` MB of zeros (ratio over 1000)."""
    return package({"word/media/bomb.bin": bytes(mb * 1024 * 1024)})


def oversized_member(mb: int = 25) -> bytes:
    return package({"word/media/big.bin": bytes(mb * 1024 * 1024)})


def lying_header(mb: int = 15, declared: int = 100) -> bytes:
    """A document.xml of `mb` MB of whitespace whose central-directory header claims `declared`
    bytes (under every cap), so only counting the real output can catch it."""
    data = bytearray(package(document=doc_xml(" " * (mb * 1024 * 1024))))
    pos = data.rfind(b"word/document.xml")  # the central directory copy
    cd = data.rfind(b"PK\x01\x02", 0, pos)
    assert cd >= 0
    struct.pack_into("<I", data, cd + 24, declared)  # uncompressed size in the central directory
    return bytes(data)


def many_members(n: int = 1000) -> bytes:
    return package({f"word/x{i}.xml": "<a/>" for i in range(n)})


def deep_xml(depth: int = 5000) -> bytes:
    return package(document=doc_xml("<w:p>" * depth + "</w:p>" * depth))


def many_elements(n: int = 150000) -> bytes:
    # Unique attributes keep the part compressible but below the ratio cap.
    return package(document=doc_xml("".join(f'<w:r w:rsidR="{i:08x}"/>' for i in range(n))))


def traversal_member(name: str = "../../evil.xml") -> bytes:
    return package({name: "<a/>"})


def many_pages_pdf(n: int = 2000) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for _ in range(n):
        doc.new_page()
    return doc.tobytes(garbage=4, deflate=True)


def text_pdf(n: int) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"Page {i} resume text", fontsize=11)
    return doc.tobytes()


def encrypted_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret", fontsize=11)
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
