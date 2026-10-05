"""Rate limiting (injected clock, no sleeping) and the bounded screening queue."""
import asyncio

import pytest

from app.api.limits import (
    BucketTable,
    RateLimitMiddleware,
    ScreeningLimiter,
    release_admission,
    screening_slot,
    screening_slots,
    try_admit,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def call(mw, method="GET", path="/v1/samples", ip="1.2.3.4", headers=None):
    scope = {"type": "http", "method": method, "path": path, "client": (ip, 5000),
             "headers": headers or []}
    sent = []

    async def send(m):
        sent.append(m)

    await mw(scope, None, send)
    start = sent[0]
    return start["status"], dict(start["headers"]), b"".join(m.get("body", b"") for m in sent[1:])



@pytest.fixture(autouse=True)
def _rate_limiting_on(monkeypatch):
    """conftest disables the app-wide limiter for the rest of the suite; these tests are
    the ones that exercise it, so switch it back on for their duration only."""
    from app import config

    monkeypatch.setitem(config.get_config()["limits"]["rate"], "enabled", True)


def make(clock=None, **kw):
    kw.setdefault("expensive_per_min", 60)  # 1 token per second
    kw.setdefault("expensive_burst", 2)
    kw.setdefault("read_per_min", 120)  # 2 tokens per second
    kw.setdefault("read_burst", 4)
    return RateLimitMiddleware(_ok_app, clock=clock or Clock(), **kw)


async def test_burst_then_429_with_standard_body_and_retry_after():
    import json

    mw = make()
    post = ("POST", "/v1/screenings")
    assert [(await call(mw, *post))[0] for _ in range(2)] == [200, 200]
    status, headers, body = await call(mw, *post)
    assert status == 429
    assert headers[b"retry-after"] == b"1"
    assert headers[b"content-type"] == b"application/json"
    data = json.loads(body)
    assert set(data) == {"code", "message", "remediation"} and data["code"] == "RATE_LIMITED"


async def test_tokens_refill_with_the_injected_clock():
    clock = Clock()
    mw = make(clock)
    post = ("POST", "/v1/screenings")
    for _ in range(2):
        await call(mw, *post)
    assert (await call(mw, *post))[0] == 429
    clock.t += 0.5
    status, headers, _ = await call(mw, *post)
    assert status == 429 and headers[b"retry-after"] == b"1"  # half a token: 0.5 s, rounded up
    clock.t += 0.5
    assert (await call(mw, *post))[0] == 200
    clock.t += 3600  # refill is capped at the burst size
    assert [(await call(mw, *post))[0] for _ in range(3)] == [200, 200, 429]


async def test_retry_after_reflects_the_wait():
    mw = make(expensive_per_min=6, expensive_burst=1)  # one token per 10 s
    await call(mw, "POST", "/v1/screenings")
    _, headers, _ = await call(mw, "POST", "/v1/screenings")
    assert headers[b"retry-after"] == b"10"


@pytest.mark.parametrize(
    "path", ["/v1/screenings", "/v1/authenticity/assess", "/v1/skills/analyze",
             "/v1/interview-questions", "/v1/screenings/"]
)
async def test_expensive_routes_use_the_small_budget(path):
    mw = make()
    assert [(await call(mw, "POST", path))[0] for _ in range(3)] == [200, 200, 429]


async def test_reads_have_a_separate_larger_budget():
    mw = make()
    for _ in range(2):
        await call(mw, "POST", "/v1/screenings")
    assert (await call(mw, "POST", "/v1/screenings"))[0] == 429
    # Exhausted expensive budget does not touch reads, and a GET of the same path is a read.
    assert [(await call(mw, "GET", "/v1/screenings/abc"))[0] for _ in range(5)] == [200] * 4 + [429]
    assert (await call(mw, "GET", "/v1/screenings", ip="8.8.8.8"))[0] == 200  # GET is a read


async def test_clients_are_independent():
    mw = make()
    post = ("POST", "/v1/screenings")
    for _ in range(3):
        await call(mw, *post, ip="10.0.0.1")
    assert (await call(mw, *post, ip="10.0.0.1"))[0] == 429
    assert (await call(mw, *post, ip="10.0.0.2"))[0] == 200


XFF = [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")]


async def test_forwarded_for_is_ignored_by_default():
    mw = make()
    post = ("POST", "/v1/screenings")
    for i in range(3):  # a spoofed header must not buy a fresh bucket
        spoof = [(b"x-forwarded-for", f"9.9.9.{i}".encode())]
        status = (await call(mw, *post, headers=spoof))[0]
    assert status == 429


async def test_trusted_proxy_uses_the_rightmost_forwarded_entry():
    mw = make(trust_proxy=True)
    post = ("POST", "/v1/screenings")
    for _ in range(2):
        await call(mw, *post, ip="10.0.0.1", headers=XFF)
    assert (await call(mw, *post, ip="10.0.0.1", headers=XFF))[0] == 429
    # Same client behind the proxy keeps one identity even when the left entry is rotated.
    rotated = [(b"x-forwarded-for", b"7.7.7.7, 203.0.113.9")]
    assert (await call(mw, *post, ip="10.0.0.1", headers=rotated))[0] == 429
    # A different real client behind the same proxy is separate.
    other = [(b"x-forwarded-for", b"203.0.113.10")]
    assert (await call(mw, *post, ip="10.0.0.1", headers=other))[0] == 200
    # Garbage in the header falls back to the socket address.
    junk = [(b"x-forwarded-for", b"not-an-ip")]
    assert mw.client_id({"client": ("10.0.0.1", 1), "headers": junk}) == "10.0.0.1"


async def test_non_http_scopes_pass_through():
    hit = []

    async def app(scope, receive, send):
        hit.append(scope["type"])

    mw = RateLimitMiddleware(app, clock=Clock())
    await mw({"type": "lifespan"}, None, None)
    await mw({"type": "websocket", "path": "/x", "client": ("1.1.1.1", 1)}, None, None)
    assert hit == ["lifespan", "websocket"]


async def test_disabled_switch_and_config_defaults(monkeypatch):
    from app import config

    mw = RateLimitMiddleware(_ok_app, clock=Clock())
    assert mw.budgets["expensive"][1] == config.cfg("limits.rate.expensive_burst")
    monkeypatch.setitem(config.get_config()["limits"]["rate"], "enabled", False)
    assert [(await call(mw, "POST", "/v1/screenings"))[0] for _ in range(20)] == [200] * 20


def test_bucket_table_memory_is_bounded_and_evicts_least_recently_used():
    t = BucketTable(3)
    for i in range(1000):
        t.take((f"ip{i}", "read"), float(i), 1.0, 5.0)
    assert len(t) == 3
    t = BucketTable(2)
    t.take(("a", "r"), 0, 1.0, 5.0)
    t.take(("b", "r"), 0, 1.0, 5.0)
    t.take(("a", "r"), 0, 1.0, 5.0)  # a is now most recent
    t.take(("c", "r"), 0, 1.0, 5.0)  # evicts b
    assert [k[0] for k in t._d] == ["a", "c"]


async def test_middleware_table_stays_bounded_under_many_clients():
    mw = make(max_clients=50)
    for i in range(500):
        await call(mw, ip=f"10.0.{i // 250}.{i % 250}")
    assert len(mw.table) == 50


# ---- bounded work queue -------------------------------------------------------------------


async def test_concurrency_never_exceeds_the_slot_count():
    lim = ScreeningLimiter()
    lim.configure(slots=3, max_pending=100)
    running = peak = 0

    async def worker():
        nonlocal running, peak
        async with lim.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 3 and running == 0 and lim.active == 0


async def test_slot_is_released_when_the_body_raises():
    lim = ScreeningLimiter()
    lim.configure(slots=1, max_pending=10)
    with pytest.raises(RuntimeError):
        async with lim.slot():
            raise RuntimeError("boom")
    assert lim.active == 0
    async with asyncio.timeout(1):  # would hang if the slot leaked
        async with lim.slot():
            assert lim.active == 1


async def test_slot_is_released_on_cancellation():
    lim = ScreeningLimiter()
    lim.configure(slots=1, max_pending=10)
    started = asyncio.Event()

    async def hold():
        async with lim.slot():
            started.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(hold())
    await started.wait()
    waiter = asyncio.create_task(_enter(lim))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    task.cancel()
    assert await asyncio.wait_for(waiter, 1) == "got"
    assert lim.active == 0


async def _enter(lim):
    async with lim.slot():
        return "got"


def test_admission_is_rejected_past_the_cap_and_recovers():
    lim = ScreeningLimiter()
    lim.configure(slots=2, max_pending=10)
    assert lim.try_admit(6) and lim.try_admit(4)
    assert lim.pending == 10
    assert not lim.try_admit(1)
    assert lim.pending == 10  # a rejection reserves nothing
    lim.release_admission(4)
    assert lim.try_admit(3) and not lim.try_admit(2)
    lim.release_admission(6)
    lim.release_admission(3)
    assert lim.pending == 0
    lim.release_admission(5)  # over-release cannot go negative
    assert lim.pending == 0


def test_oversized_screening_is_admitted_only_into_an_empty_queue():
    lim = ScreeningLimiter()
    lim.configure(slots=2, max_pending=5)
    assert lim.try_admit(8)
    assert not lim.try_admit(1)
    lim.release_admission(8)
    assert lim.try_admit(1) and not lim.try_admit(8)


async def test_pending_recovers_after_a_failed_run():
    lim = ScreeningLimiter()
    lim.configure(slots=2, max_pending=4)

    async def run(n, fail):
        try:
            for _ in range(n):
                async with lim.slot():
                    if fail:
                        raise ValueError("candidate failed")
        finally:
            lim.release_admission(n)

    assert lim.try_admit(4)
    assert not lim.try_admit(1)
    with pytest.raises(ValueError):
        await run(4, fail=True)
    assert lim.pending == 0 and lim.active == 0
    assert lim.try_admit(4)


async def test_waiting_work_is_bounded_by_admission_not_by_slots():
    lim = ScreeningLimiter()
    lim.configure(slots=1, max_pending=3)
    gate = asyncio.Event()

    async def cand():
        async with lim.slot():
            await gate.wait()

    assert lim.try_admit(3)
    tasks = [asyncio.create_task(cand()) for _ in range(3)]
    await asyncio.sleep(0.01)
    assert lim.active == 1 and lim.pending == 3
    assert not lim.try_admit(1)
    gate.set()
    await asyncio.gather(*tasks)
    lim.release_admission(3)
    assert lim.try_admit(3)


def test_rejection_response_is_503_with_retry_after_and_standard_body():
    import json

    r = ScreeningLimiter().rejection_response()
    assert r.status_code == 503 and r.headers["retry-after"] == "15"
    body = json.loads(r.body)
    assert body["code"] == "QUEUE_FULL" and body["message"] and body["remediation"]


async def test_module_level_helpers_share_one_limiter():
    screening_slots.reset()
    screening_slots.configure(slots=2, max_pending=2)
    try:
        assert try_admit(2) and not try_admit(1)
        async with screening_slot():
            assert screening_slots.active == 1
        release_admission(2)
        assert screening_slots.pending == 0
    finally:
        screening_slots.configure()
        screening_slots.reset()


async def test_semaphore_is_rebuilt_for_a_new_event_loop():
    lim = ScreeningLimiter()
    lim.configure(slots=1, max_pending=5)
    async with lim.slot():
        pass
    first = lim._sem
    lim._loop = object()  # as if the previous loop had been closed
    async with lim.slot():
        pass
    assert lim._sem is not first


def test_full_queue_rejects_a_screening_through_the_api(monkeypatch, tmp_path):
    """Wiring: POST /v1/screenings is refused with 503 + Retry-After when admission fails,
    before anything is stored."""
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.main import app

    monkeypatch.setenv("SCREENING_DB_PATH", str(tmp_path / "q.db"))
    monkeypatch.setattr(routes, "try_admit", lambda n: False)
    with TestClient(app) as client:
        r = client.post("/v1/screenings", data={"jd_text": "Python developer", "pasted_resumes": ["Jane\nPython"]})
    assert r.status_code == 503
    assert r.json()["code"] == "QUEUE_FULL" and "retry-after" in {k.lower() for k in r.headers}
