"""Portfolio evidence collector (Module B Stage 2).

httpx + a dependency-light text/link extractor (falls back without
BeautifulSoup/trafilatura, same zero-cost-first pattern as the PDF loader).
Respects robots.txt with a minimal stdlib-only parser (User-agent: * only -
see the docstring on `_allowed`). 10s timeout. Live-demo links are checked for
a 2xx status; a dead link is WEAK evidence only, never a contradiction
(Spec 11 Stage 2 - dead link != lie).

Outbound-request safety (audit fix): the candidate controls the URL, so the real fetch
(`_default_fetch`) is a guarded one. Only http/https; the host must not be linkedin.com
(Spec 2.9: no scraping of linkedin.com); every address the host resolves to must be public
(no private, loopback, link-local, multicast, reserved or unspecified IPv4/IPv6); redirects
are followed by hand, at most `http.max_redirects` hops, and each hop is re-checked; and the
response body is read as a stream and cut off at `http.max_body_bytes`. Injected fetches (tests)
get the static URL checks only, never a DNS lookup. Known limit: the check resolves the name and
then httpx resolves it again to connect, so a DNS-rebinding race is narrowed, not closed
(ASSUMPTIONS would need a pinned-IP transport to close it).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from ....config import cfg
from ....db.cache import cache_enabled, cached_fetch
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


class BlockedURL(Exception):
    """The URL is refused before any request is made (or before a redirect is followed)."""


_LINKEDIN_HOSTS = ("linkedin.com", "lnkd.in")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")


def _address_problem(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified or not ip.is_global):
        return f"{ip} is not a public address"
    return None


def url_problem(url: str) -> str | None:
    """Static checks that need no network: scheme, linkedin.com, literal and obfuscated IPs,
    local names. Returns the reason the URL must not be fetched, or None."""
    try:
        parts = urlparse(url)
        host = (parts.hostname or "").rstrip(".").lower()
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return "the portfolio URL is malformed"
    if parts.scheme not in ("http", "https"):
        return "only http and https portfolio URLs are fetched"
    if not host:
        return "the portfolio URL has no host"
    if any(host == h or host.endswith("." + h) for h in _LINKEDIN_HOSTS):
        return ("linkedin.com is never fetched (no scraping); "
                "upload the candidate's LinkedIn export instead")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return "local host names are not fetched"
    try:
        return _address_problem(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:  # 2130706433, 0x7f.1, 127.1: forms the OS resolver would read as an IPv4 address
        return _address_problem(ipaddress.IPv4Address(socket.inet_aton(host)))
    except (OSError, ValueError):
        return None


async def _resolve(host: str, port: int) -> list[str]:
    import asyncio

    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]).split("%")[0] for i in infos})


async def _assert_public(url: str) -> None:
    """Raises BlockedURL unless the URL passes the static checks AND every address its host
    resolves to is public. Run on the first URL and again on every redirect target."""
    if reason := url_problem(url):
        raise BlockedURL(reason)
    parts = urlparse(url)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
        return  # a literal public IP: nothing to resolve
    except ValueError:
        pass
    try:
        addrs = await _resolve(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise BlockedURL(f"the host could not be resolved ({exc.__class__.__name__})") from exc
    if not addrs:
        raise BlockedURL("the host did not resolve to any address")
    for a in addrs:
        try:
            problem = _address_problem(ipaddress.ip_address(a))
        except ValueError:
            problem = f"{a} is not a valid address"
        if problem:
            raise BlockedURL(f"the host resolves to a non-public address: {problem}")


def _client():  # seam: tests substitute a client with a mock transport
    import httpx

    return httpx.AsyncClient(timeout=float(cfg("http.timeout_s", 10)), follow_redirects=False)


async def _default_fetch(url: str) -> tuple[int, str]:
    """The guarded real fetch (see module docstring). Raises BlockedURL for a refused URL or
    redirect target; returns the body cut off at the byte cap."""
    max_hops = int(cfg("http.max_redirects", 3))
    cap = int(cfg("http.max_body_bytes", 2 * 1024 * 1024))
    current = url
    async with _client() as client:
        for _ in range(max_hops + 1):
            await _assert_public(current)
            async with client.stream("GET", current) as resp:
                location = resp.headers.get("location")
                if resp.status_code in (301, 302, 303, 307, 308) and location:
                    current = urljoin(current, location)
                    continue
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) >= cap:
                        del body[cap:]
                        break  # stop reading: the rest of the body is never downloaded
                return resp.status_code, body.decode(resp.encoding or "utf-8", "replace")
    raise BlockedURL(f"more than {max_hops} redirects")


def _describe(exc: Exception) -> str:
    if "timeout" in type(exc).__name__.lower() or isinstance(exc, TimeoutError):
        return "the portfolio site timed out"
    return str(exc) or type(exc).__name__


async def collect_portfolio(
    url: str | None, *, fetch: Fetch | None = None, max_links_checked: int = 3
) -> PortfolioEvidence:
    """Stage 2 portfolio collector. Never raises: failures become status='error'."""
    if not url:
        return PortfolioEvidence(url="", status="missing")
    if fetch is None:
        # The default fetch is the cache seam; injected fetches are used as given.
        fetch = cached_fetch(_default_fetch) if cache_enabled() else _default_fetch
    url_full = url if "://" in url else f"https://{url}"
    if reason := url_problem(url_full):
        return PortfolioEvidence(url=url, status="error", error=reason)
    parsed = urlparse(url_full)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if bool(cfg("http.respect_robots", True)):
        try:
            robots_status, robots_txt = await fetch(urljoin(origin, "/robots.txt"))
            if robots_status == 200 and not _allowed(robots_txt, parsed.path or "/"):
                return PortfolioEvidence(url=url, status="disallowed",
                                         error="robots.txt disallows this path")
        except Exception:
            pass

    try:
        status, html = await fetch(url_full)
    except Exception as exc:
        return PortfolioEvidence(url=url, status="error", error=_describe(exc))
    if status != 200:
        return PortfolioEvidence(url=url, status="error", error=f"HTTP {status}")

    text = _extract_with_bs4(html) or _strip_html(html)
    projects = [_strip_html(t)[:120] for t in _TITLE_TAGS.findall(html)][:10]
    words = set(re.findall(r"[a-z0-9+#.]+", text.lower()))
    tech = sorted({c for c in (canonical(w) for w in words) if c})
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
        if reason := url_problem(link):
            checks.append({"url": link, "status_code": None, "ok": False, "error": reason})
            continue
        try:
            s, _ = await fetch(link)
            checks.append({"url": link, "status_code": s, "ok": 200 <= s < 300})
        except Exception as exc:
            checks.append({"url": link, "status_code": None, "ok": False, "error": _describe(exc)})

    return PortfolioEvidence(
        url=url, status="ok", text=text[:5000], projects=projects, tech_mentions=tech,
        outbound_links=outbound, live_demo_checks=checks,
    )
