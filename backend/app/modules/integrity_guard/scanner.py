"""Module A deterministic scanner. Code detects; the LLM only explains (Spec 8.1)."""
from __future__ import annotations
import re
from typing import Any

from ...config import cfg

INJECTION_LEXICON = [
    r"ignore (?:all )?(?:previous|prior|above) instructions",
    r"disregard (?:the |all )?(?:previous|prior|above|missing)",
    r"rate (?:this )?candidate \d+\s*/\s*\d+",
    r"(?:you must|please) recommend this candidate",
    r"score this (?:resume|candidate) (?:as )?(?:10|100|highly|maximum)",
    r"shortlist this candidate",
    r"you are (?:a|an|now)\b",
    r"system prompt",
    r"as an ai (?:language )?model",
    r"the (?:ideal|perfect) candidate for this role is",
    r"output only",
]
_INJECTION_RE = re.compile("|".join(INJECTION_LEXICON), re.I)
TECH_KEYWORDS = set(
    """python java javascript typescript react node fastapi django flask sql postgresql mysql
    mongodb kafka rabbitmq docker kubernetes aws azure gcp terraform redis graphql rest git
    ci cd microservices pytorch tensorflow spark airflow""".split()
)


def _luminance(rgb: int) -> float:
    def ch(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(fg: int, bg: int = 0xFFFFFF) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def is_hidden(span: Any, page_size: tuple[float, float]) -> bool:
    """A span a human reader cannot see: low contrast, sub-readable, or off-page."""
    if getattr(span, "render_mode", 0) == 3:
        return True
    if span.size < float(cfg("integrity.hidden_font_pt_max", 4.0)):
        return True
    if contrast_ratio(span.color) < float(cfg("integrity.min_contrast_ratio", 1.3)):
        return True
    x0, y0, x1, y1 = span.bbox
    if (x0, y0, x1, y1) == (0, 0, 0, 0):
        return False
    m = float(cfg("integrity.offpage_margin_pt", 2))
    w, h = page_size
    return x1 < -m or y1 < -m or x0 > w + m or y0 > h + m


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#.]+", text.lower())


def longest_common_run(a: list[str], b: list[str]) -> tuple[int, int]:
    """Returns (longest run length, total words in runs >= min_run)."""
    min_run = int(cfg("integrity.jd_clone_min_run_words", 12))
    bset: dict[tuple[str, ...], bool] = {}
    for i in range(len(b) - min_run + 1):
        bset[tuple(b[i : i + min_run])] = True
    longest, total, i = 0, 0, 0
    while i <= len(a) - min_run:
        if tuple(a[i : i + min_run]) in bset:
            run = min_run
            while (
                i + run < len(a)
                and tuple(a[i + run - min_run + 1 : i + run + 1]) in bset
            ):
                run += 1
            longest = max(longest, run)
            total += run
            i += run
        else:
            i += 1
    return longest, total


def scan(doc: Any, jd_text: str = "") -> dict:
    """Returns the scanner report of Spec 8.1."""
    flags: list[dict] = []
    hidden_spans = [s for s in doc.spans if is_hidden(s, doc.page_size)]
    ocr_spans = [s for s in hidden_spans if getattr(s, "render_mode", 0) == 3]
    non_ocr_hidden = [s for s in hidden_spans if getattr(s, "render_mode", 0) != 3]
    hidden_text = " ".join(s.text for s in non_ocr_hidden).strip()

    if ocr_spans and len(ocr_spans) >= max(1, len(doc.spans) * float(
        cfg("integrity.ocr_page_coverage_min", 0.80)
    )):
        flags.append(
            {
                "code": "OCR_LAYER",
                "severity": "info",
                "title": "Scanned-document text layer",
                "detail": "Invisible text spans the page, consistent with a scanned document.",
                "evidence": f"{len(ocr_spans)} invisible spans over {doc.pages} page(s)",
            }
        )

    if hidden_text:
        flags.append(
            {
                "code": "HIDDEN_TEXT",
                "severity": "high",
                "title": "Text not visible to a reader",
                "detail": f"{len(hidden_text.split())} words are present in the file but not visible.",
                "evidence": _quote(hidden_text),
            }
        )
        if _INJECTION_RE.search(hidden_text):
            flags.append(
                {
                    "code": "INJECTION_HIDDEN",
                    "severity": "high",
                    "title": "Instructions aimed at an automated screener",
                    "detail": "The hidden text addresses an AI system directly.",
                    "evidence": _quote(hidden_text),
                }
            )

    if _INJECTION_RE.search(doc.visible_text):
        flags.append(
            {
                "code": "INJECTION_VISIBLE",
                "severity": "low",
                "title": "Instruction-like phrasing in the visible text",
                "detail": "May be innocent wording; carries little weight on its own.",
                "evidence": _quote(_INJECTION_RE.search(doc.visible_text).group(0)),
            }
        )

    if jd_text:
        all_text = doc.visible_text + " " + hidden_text
        longest, total = longest_common_run(_norm_words(all_text), _norm_words(jd_text))
        if total >= int(cfg("integrity.jd_clone_min_total_words", 25)):
            flags.append(
                {
                    "code": "JD_CLONE",
                    "severity": "high" if total >= 60 else "medium",
                    "title": "Job description copied into the file",
                    "detail": f"{total} words match the job description verbatim "
                    f"(longest run {longest} words).",
                    "evidence": f"longest verbatim run: {longest} words",
                }
            )

    meta_blob = " ".join(doc.metadata.values()).lower()
    meta_hits = sorted({w for w in _norm_words(meta_blob) if w in TECH_KEYWORDS})
    if len(meta_hits) >= int(cfg("integrity.metadata_keyword_min", 8)):
        flags.append(
            {
                "code": "METADATA_STUFF",
                "severity": "medium",
                "title": "Keywords packed into file metadata",
                "detail": f"{len(meta_hits)} technical keywords appear in metadata fields "
                "that never render on the page.",
                "evidence": ", ".join(meta_hits[:10]),
            }
        )

    texts = [t for t in doc.raw_text_by_parser.values() if t and t.strip()]
    if len(doc.raw_text_by_parser) >= 2 and len(texts) >= 2:
        a, b = (set(_norm_words(texts[0])), set(_norm_words(texts[1])))
        union = a | b
        if union:
            divergence = len(a ^ b) / len(union)
            if divergence > float(cfg("integrity.parser_divergence_ratio", 0.08)):
                flags.append(
                    {
                        "code": "PARSER_DIVERGENCE",
                        "severity": "medium",
                        "title": "Two readers disagree about this document",
                        "detail": f"The two parsers disagree on {divergence:.0%} of the words.",
                        "evidence": f"divergence ratio {divergence:.3f}",
                    }
                )

    verdict = _verdict(flags)
    penalties = cfg("integrity.penalties")
    return {
        "verdict": verdict,
        "confidence": _confidence(flags, verdict),
        "penalty": float(penalties[verdict]),
        "flags": sorted(flags, key=lambda f: f["code"]),
        "hidden_text": hidden_text,
        "stats": {
            "hidden_words": len(hidden_text.split()),
            "pages": doc.pages,
            "backends": sorted(doc.raw_text_by_parser),
        },
    }


def _verdict(flags: list[dict]) -> str:
    codes = {f["code"] for f in flags}
    highs = {f["code"] for f in flags if f["severity"] == "high"}
    if "INJECTION_HIDDEN" in codes or len(highs) >= 2:
        return "attack"
    if any(f["severity"] in ("medium", "high") for f in flags if f["code"] != "OCR_LAYER"):
        return "suspicious"
    return "clean"


def _confidence(flags: list[dict], verdict: str) -> int:
    scored = [f for f in flags if f["code"] != "OCR_LAYER"]
    if verdict == "clean":
        return 95 if not scored else 70
    return min(99, 55 + 15 * len(scored))


def _quote(text: str) -> str:
    words = text.split()[: int(cfg("integrity.max_hidden_quote_words", 15))]
    return " ".join(words) + ("..." if len(text.split()) > len(words) else "")
