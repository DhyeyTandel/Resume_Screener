"""Portfolio fetcher safety (audit fix; Spec 2.9 no linkedin.com scraping, SSRF guard).
No test here touches the network: the resolver and the HTTP client are replaced."""

import httpx
import pytest

from app.modules.authenticity_engine.collectors import portfolio as pf
from app.modules.authenticity_engine.collectors.portfolio import collect_portfolio
from tests.fixtures.portfolio_fixtures import make_fetch


def fake_client(handler):
    def make():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return make


@pytest.fixture
def resolver(monkeypatch):
    """Host -> addresses table; an unknown host fails to resolve. Records every lookup."""
    table = {"priya.dev": ["93.184.216.34"], "files.example": ["93.184.216.34"]}
    seen = []

    async def resolve(host, port):
        seen.append(host)
        if host not in table:
            raise OSError("no such host")
        return table[host]

    monkeypatch.setattr(pf, "_resolve", resolve)
    resolve.table, resolve.seen = table, seen
    return resolve


class Spy:
    def __init__(self):
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        return 200, "<html></html>"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8000/admin", "https://10.0.0.5/", "http://10.255.255.255",
    "http://172.16.0.1/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://[fd00::1]/",
    "http://0.0.0.0/", "http://[::]/", "http://224.0.0.1/", "http://240.0.0.1/",
    "http://100.64.0.1/", "http://2130706433/", "http://0x7f.1/", "http://127.1/",
    "http://localhost/", "http://app.localhost/", "http://db.internal/",
    "ftp://priya.dev/", "file:///etc/passwd", "gopher://priya.dev/",
])
async def test_non_public_or_non_http_urls_are_blocked_without_any_fetch(url):
    spy = Spy()
    out = await collect_portfolio(url, fetch=spy)
    assert out.status == "error" and out.error
    assert spy.calls == []


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/in/someone", "linkedin.com/in/someone", "https://LinkedIn.com/in/x",
    "https://uk.linkedin.com/in/x", "https://lnkd.in/abc",
])
async def test_linkedin_is_never_fetched(url):
    spy = Spy()
    out = await collect_portfolio(url, fetch=spy)
    assert out.status == "error"
    assert "linkedin" in out.error.lower() and "no scraping" in out.error.lower()
    assert spy.calls == []


async def test_hostname_resolving_to_a_private_ip_is_blocked(monkeypatch, resolver):
    resolver.table["sneaky.example"] = ["10.1.2.3"]
    hits = []
    monkeypatch.setattr(pf, "_client", fake_client(lambda r: hits.append(r) or httpx.Response(200)))
    out = await collect_portfolio("https://sneaky.example/", fetch=pf._default_fetch)
    assert out.status == "error" and "non-public" in out.error
    assert hits == []  # no request was ever sent


async def test_hostname_with_one_private_address_among_public_ones_is_blocked(monkeypatch, resolver):
    resolver.table["mixed.example"] = ["93.184.216.34", "::1"]
    monkeypatch.setattr(pf, "_client", fake_client(lambda r: httpx.Response(200)))
    out = await collect_portfolio("https://mixed.example/", fetch=pf._default_fetch)
    assert out.status == "error"


async def test_redirect_to_a_private_ip_is_blocked(monkeypatch, resolver):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    monkeypatch.setattr(pf, "_client", fake_client(handler))
    out = await collect_portfolio("https://priya.dev/", fetch=pf._default_fetch)
    assert out.status == "error" and "public address" in out.error
    assert not any("169.254" in u for u in seen)  # the metadata address was never requested


async def test_redirect_to_a_hostname_that_resolves_privately_is_blocked(monkeypatch, resolver):
    resolver.table["internal-admin.example"] = ["192.168.0.10"]

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.host == "priya.dev":
            return httpx.Response(301, headers={"location": "https://internal-admin.example/x"})
        return httpx.Response(200, text="secret")

    monkeypatch.setattr(pf, "_client", fake_client(handler))
    out = await collect_portfolio("https://priya.dev/", fetch=pf._default_fetch)
    assert out.status == "error" and "non-public" in out.error


async def test_redirect_to_linkedin_is_blocked(monkeypatch, resolver):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://www.linkedin.com/in/someone"})

    monkeypatch.setattr(pf, "_client", fake_client(handler))
    out = await collect_portfolio("https://priya.dev/", fetch=pf._default_fetch)
    assert out.status == "error" and "linkedin" in out.error.lower()


async def test_redirect_loop_is_cut_off_at_the_hop_cap(monkeypatch, resolver):
    count = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        count.append(1)
        return httpx.Response(302, headers={"location": "https://priya.dev/again"})

    monkeypatch.setattr(pf, "_client", fake_client(handler))
    out = await collect_portfolio("https://priya.dev/", fetch=pf._default_fetch)
    assert out.status == "error" and "redirects" in out.error
    assert len(count) == 4  # first request plus http.max_redirects (3) hops


async def test_a_safe_redirect_is_followed(monkeypatch, resolver):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/":
            return httpx.Response(301, headers={"location": "/home"})
        return httpx.Response(200, text="<html><h1>Ledger Service</h1> FastAPI</html>")

    monkeypatch.setattr(pf, "_client", fake_client(handler))
    out = await collect_portfolio("https://priya.dev/", fetch=pf._default_fetch)
    assert out.status == "ok" and "Ledger Service" in out.projects


async def test_oversized_body_is_cut_off_and_not_downloaded_in_full(monkeypatch, resolver):
    pulled = []

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10_000):  # 10,000 x 64 KiB = 640 MiB if it were all read
                pulled.append(1)
                yield b"a" * 65536

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, stream=Endless())

    monkeypatch.setattr(pf, "_client", fake_client(handler))
    status, text = await pf._default_fetch("https://priya.dev/")
    assert status == 200
    assert len(text) == 2 * 1024 * 1024  # http.max_body_bytes default
    assert len(pulled) <= 40  # stopped reading near the cap


async def test_legitimate_fixture_fetches_still_work_and_need_no_dns(monkeypatch):
    async def no_dns(host, port):
        raise AssertionError("an injected fetch must not trigger a DNS lookup")

    monkeypatch.setattr(pf, "_resolve", no_dns)
    out = await collect_portfolio("https://priya.dev", fetch=make_fetch())
    assert out.status == "ok" and "Ledger Service" in out.projects
    assert any(c["ok"] for c in out.live_demo_checks)


async def test_a_demo_link_to_a_private_address_is_not_fetched():
    seen = []
    base = make_fetch()

    async def fetch(url):
        seen.append(url)
        if url.endswith("priya.dev") or url.rstrip("/").endswith("priya.dev"):
            return 200, '<h1>X</h1><a href="http://127.0.0.1:8080/demo">demo</a>'
        return await base(url)

    out = await collect_portfolio("https://priya.dev", fetch=fetch)
    assert out.status == "ok"
    assert not any("127.0.0.1" in u for u in seen)
    assert out.live_demo_checks and out.live_demo_checks[0]["ok"] is False


# ------------------------------------------------------------------ Spec 17: portfolio timeout
@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), TimeoutError("t"), TimeoutError(),
])
async def test_portfolio_timeout_gives_a_clean_error_status(exc):
    async def fetch(url):
        if url.endswith("/robots.txt"):
            return 404, ""
        raise exc

    out = await collect_portfolio("https://priya.dev", fetch=fetch)
    assert out.status == "error"
    assert out.error == "the portfolio site timed out"
    assert out.text == "" and out.projects == []


async def test_timeout_in_the_real_fetch_path_is_also_clean(monkeypatch, resolver):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    monkeypatch.setattr(pf, "_client", fake_client(handler))
    out = await collect_portfolio("https://priya.dev", fetch=pf._default_fetch)
    assert out.status == "error" and out.error == "the portfolio site timed out"
