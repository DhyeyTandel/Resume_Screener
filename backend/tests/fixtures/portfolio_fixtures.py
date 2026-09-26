"""Recorded portfolio-site responses for tests. No live network calls in CI."""
from __future__ import annotations

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
ROBOTS_DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private\n"

HOME_HTML = """
<html><head><title>Priya Raman</title></head><body>
<h1>Ledger Service</h1>
<p>A FastAPI and PostgreSQL settlement ledger, deployed with Docker.</p>
<a href="https://ledger-demo.vercel.app">Live demo</a>
<a href="https://github.com/priya/ledger-service">Source</a>
<h2>Reporting Dashboard</h2>
<p>A Kafka-backed reporting pipeline written in Python.</p>
<a href="https://dead-demo.herokuapp.com">Broken demo</a>
</body></html>
"""


def make_fetch(*, robots: str = ROBOTS_ALLOW_ALL, home_status: int = 200, demo_status: int = 200,
              dead_demo_status: int = 503):
    async def fetch(url: str) -> tuple[int, str]:
        if url.endswith("/robots.txt"):
            return 200, robots
        if "ledger-demo.vercel.app" in url:
            return demo_status, "<html>demo up</html>"
        if "dead-demo.herokuapp.com" in url:
            return dead_demo_status, ""
        if "priya.dev" in url or url.rstrip("/").endswith("priya.dev"):
            return home_status, HOME_HTML
        return 404, ""

    return fetch
