"""Portfolio evidence collector (Module B Stage 2).

httpx + a dependency-light text/link extractor (falls back without
BeautifulSoup/trafilatura, same zero-cost-first pattern as the PDF loader).
Respects robots.txt with a minimal stdlib-only parser (User-agent: * only -
see the docstring on `_allowed`). 10s timeout. Live-demo links are checked for
a 2xx status; a dead link is WEAK evidence only, never a contradiction
(Spec 11 Stage 2 - dead link != lie).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse

from ....config import cfg
from ...skill_intelligence.transfer import canonical

Fetch = Callable[[str], Awaitable[tuple[int, str]]]

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_LINK = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TITLE_TAGS = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.S | re.I)


@dataclass
class PortfolioEvidence:
    url: str
    status: str  # ok | missing | no_consent | error | disallowed
    text: str = ""
    projects: list[str] = field(default_factory=list)
    tech_mentions: list[str] = field(default_factory=list)
    outbound_links: list[str] = field(default_factory=list)
    live_demo_checks: list[dict] = field(default_factory=list)
    error: str | None = None


def _strip_html(html: str) -> str:
    html = _SCRIPT_STYLE.sub(" ", html)
    text = _TAG.sub(" ", html)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_with_bs4(html: str) -> str | None:
    try:
        from bs4 import BeautifulSoup  # optional dependency

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    except ImportError:
        return None


def _allowed(robots_txt: str, path: str) -> bool:
    """Minimal robots.txt check for User-agent: * only (Assumption: this build
    does not identify itself with a custom UA, so a site-specific rule for
    another agent is not honoured - a conservative simplification, not a
    workaround, since '*' rules still block us when a site wants them to)."""
    if not robots_txt.strip():
        return True
    applies, disallows, allows = False, [], []
    for line in robots_txt.splitlines():
        line = line.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            applies = val == "*"
        elif applies and key == "disallow" and val:
            disallows.append(val)
        elif applies and key == "allow" and val:
            allows.append(val)
    best_allow = max((a for a in allows if path.startswith(a)), key=len, default="")
    best_disallow = max((d for d in disallows if path.startswith(d)), key=len, default="")
    return len(best_allow) >= len(best_disallow) if best_disallow else True


async def _default_fetch(url: str) -> tuple[int, str]:
    import httpx

    async with httpx.AsyncClient(
        timeout=float(cfg("http.timeout_s", 10)), follow_redirects=True
    ) as client:
        resp = await client.get(url)
        return resp.status_code, resp.text


async def collect_portfolio(
    url: str | None, *, fetch: Fetch | None = None, max_links_checked: int = 3
) -> PortfolioEvidence:
    """Stage 2 portfolio collector. Never raises: failures become status='error'."""
    if not url:
        return PortfolioEvidence(url="", status="missing")
    fetch = fetch or _default_fetch
    parsed = urlparse(url if "://" in url else f"https://{url}")
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if bool(cfg("http.respect_robots", True)):
        try:
            robots_status, robots_txt = await fetch(urljoin(origin, "/robots.txt"))
            if robots_status == 200 and not _allowed(robots_txt, parsed.path or "/"):
                return PortfolioEvidence(url=url, status="disallowed",
                                         error="robots.txt disallows this path")
        except Exception:  # noqa: BLE001 - robots.txt fetch failure never blocks the page fetch
            pass

    try:
        status, html = await fetch(url if "://" in url else f"https://{url}")
    except Exception as exc:  # noqa: BLE001
        return PortfolioEvidence(url=url, status="error", error=str(exc))
    if status != 200:
        return PortfolioEvidence(url=url, status="error", error=f"HTTP {status}")

    text = _extract_with_bs4(html) or _strip_html(html)
    projects = [_strip_html(t)[:120] for t in _TITLE_TAGS.findall(html)][:10]
    words = set(re.findall(r"[a-z0-9+#.]+", text.lower()))
    tech = sorted({canonical(w) for w in words if canonical(w)})
    links = sorted(set(_LINK.findall(html)))[:30]
    outbound = [
        urljoin(url, link) for link in links
        if link and not link.startswith("#") and not link.lower().startswith("mailto:")
    ]

    checks = []
    demo_like = [
        link for link in outbound
        if any(k in link.lower() for k in ("demo", "live", "app.", "vercel", "netlify", "herokuapp"))
    ][:max_links_checked]
    for link in demo_like:
        try:
            s, _ = await fetch(link)
            checks.append({"url": link, "status_code": s, "ok": 200 <= s < 300})
        except Exception as exc:  # noqa: BLE001 - a dead link is weak evidence, never a crash
            checks.append({"url": link, "status_code": None, "ok": False, "error": str(exc)})

    return PortfolioEvidence(
        url=url, status="ok", text=text[:5000], projects=projects, tech_mentions=tech,
        outbound_links=outbound, live_demo_checks=checks,
    )
