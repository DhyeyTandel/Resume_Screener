"""Real provider code paths, exercised only through httpx.MockTransport (no network, no key)."""
import asyncio
import json
import time

import httpx
import pytest

from app.config import cfg
from app.llm.client import LLMClient
from app.modules.interview_questions.generator import generate_interview_questions

KEY = "sk-ant-TEST-SECRET-123"
GOOD = {"summary": "s", "strengths": [], "risks": [], "rationale": "r"}
PAYLOAD = {"matched": ["Python"], "missing": [], "base_score": 80, "total_requirements": 1}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setattr(time, "sleep", _boom)


def _boom(*a, **k):
    raise AssertionError("time.sleep must never be called")


def anth(text, stop="end_turn", usage=(11, 7)):
    return httpx.Response(200, json={
        "content": [{"type": "text", "text": text}], "stop_reason": stop,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}})


def make(handler, provider="anthropic"):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    c = LLMClient(provider, transport=httpx.MockTransport(handler), sleep=fake_sleep)
    c.chain = [provider, "mock"]
    return c, sleeps


async def test_anthropic_request_shape_and_usage():
    seen = []

    def handler(req):
        seen.append(req)
        return anth(json.dumps(GOOD))

    c, _ = make(handler)
    r = await c.complete_json("SYS {braces}", PAYLOAD, task="summary", temperature=0.3,
                              max_tokens=900)
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == cfg("llm.anthropic.model")
    assert body["system"] == "SYS {braces}"
    assert body["max_tokens"] == 900 and body["temperature"] == 0.3
    assert body["messages"] == [{"role": "user", "content": c.prompts[0]["user"]}]
    assert json.loads(body["messages"][0]["content"]) == PAYLOAD
    assert r.provider == "anthropic" and r.data == GOOD
    assert r.usage == {"input_tokens": 11, "output_tokens": 7}
    assert r.stop_reason == "end_turn" and r.attempts == 1


async def test_fenced_json_and_non_text_blocks():
    def handler(req):
        return httpx.Response(200, json={
            "content": [{"type": "thinking", "thinking": "x"},
                        {"type": "text", "text": "```json\n" + json.dumps(GOOD) + "\n```"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 2}})

    c, _ = make(handler)
    assert (await c.complete_json("s", PAYLOAD, task="summary")).data == GOOD


async def test_429_then_200_two_attempts_and_async_sleep_awaited():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429) if len(calls) == 1 else anth(json.dumps(GOOD))

    c, sleeps = make(handler)
    r = await c.complete_json("s", PAYLOAD, task="summary")
    assert r.attempts == 2 and r.provider == "anthropic"
    assert sleeps == [1]


async def test_default_sleep_is_asyncio_sleep(monkeypatch):
    awaited = []

    async def rec(s):
        awaited.append(s)

    monkeypatch.setattr(asyncio, "sleep", rec)
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(503) if len(n) < 3 else anth(json.dumps(GOOD))

    c = LLMClient("anthropic", transport=httpx.MockTransport(handler))  # default sleep
    r = await c.complete_json("s", PAYLOAD, task="summary")
    assert awaited == [1, 2] and r.attempts == 3


async def test_500_every_attempt_falls_back_to_mock():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(500, text="oops")

    c, sleeps = make(handler)
    r = await c.complete_json("s", PAYLOAD, task="summary")
    assert len(n) == 3 and sleeps == [1, 2]
    assert r.provider == "mock" and c.mock_mode
    assert r.errors and "anthropic" in r.errors[0] and KEY not in str(r.errors)


async def test_timeout_is_retried():
    n = []

    def handler(req):
        n.append(1)
        if len(n) == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return anth(json.dumps(GOOD))

    c, sleeps = make(handler)
    r = await c.complete_json("s", PAYLOAD, task="summary")
    assert r.attempts == 2 and sleeps == [1]


async def test_4xx_not_retried_falls_through():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(401, text=f"bad key {KEY}")

    c, sleeps = make(handler)
    r = await c.complete_json("s", PAYLOAD, task="summary")
    assert len(n) == 1 and sleeps == [] and r.provider == "mock"
    assert KEY not in str(r.errors)


async def test_parse_failure_retries_with_strict_suffix():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return anth("not json at all") if len(bodies) == 1 else anth(json.dumps(GOOD))

    c, sleeps = make(handler)
    r = await c.complete_json("SYS", PAYLOAD, task="summary")
    user = c.prompts[0]["user"]
    assert r.attempts == 2 and sleeps == []
    assert bodies[0]["messages"][0]["content"] == user
    assert bodies[1]["messages"][0]["content"] == user + "\n\nSTRICT: Return ONLY the JSON object. No other text."
    assert bodies[1]["system"] == "SYS"


async def test_key_never_in_exception_message():
    def handler(req):
        return httpx.Response(500, text=f"echo {req.headers['x-api-key']}")

    c, _ = make(handler)
    with pytest.raises(RuntimeError) as ei:
        await c._http_json("anthropic", "s", "u", None, None, None)
    assert KEY not in str(ei.value) and KEY not in repr(ei.value)

    def handler2(req):
        return anth(f"garbage {KEY}")

    c2, _ = make(handler2)
    with pytest.raises(RuntimeError) as ei2:
        await c2._http_json("anthropic", "s", "u", None, None, None)
    assert KEY not in str(ei2.value)
    r = await c2.complete_json("s", PAYLOAD, task="summary")
    assert KEY not in json.dumps(r.__dict__, default=str)


async def test_missing_key_falls_to_mock_without_http(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    def handler(req):
        raise AssertionError("no HTTP without a key")

    c, _ = make(handler)
    assert (await c.complete_json("s", PAYLOAD, task="summary")).provider == "mock"


async def test_ollama_round_trip():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        assert req.url.path == "/api/chat"
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": json.dumps(GOOD)},
            "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 9})

    c, _ = make(handler, "ollama")
    r = await c.complete_json("SYS", PAYLOAD, task="summary", temperature=0, max_tokens=300)
    b = seen[0]
    assert b["model"] == cfg("llm.ollama.model") and b["stream"] is False
    assert b["format"] == "json"
    assert b["options"] == {"temperature": 0, "seed": 7, "num_predict": 300}
    assert b["messages"][0] == {"role": "system", "content": "SYS"}
    assert r.provider == "ollama" and r.data == GOOD
    assert r.usage == {"input_tokens": 5, "output_tokens": 9}


async def test_ollama_down_falls_through_without_stalling():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    c, sleeps = make(handler, "ollama")
    r = await c.complete_json("s", PAYLOAD, task="summary")
    assert r.provider == "mock" and sleeps == []


def test_no_time_sleep_in_client_source():
    import inspect

    from app.llm import client
    assert "time.sleep" not in inspect.getsource(client)


# ---------------------------------------------------------------- Module D
REQS = [{"skill": "Kafka", "evidence_level": "claimed", "jd_priority": "must_have"},
        {"skill": "Go", "evidence_level": "not_demonstrated", "jd_priority": "must_have"}]
QS = {"interview_questions": [
    {"skill": s, "evidence_level": lvl, "question": "q?", "purpose": "p", "risk_if_unanswered": "r"}
    for s, lvl in (("Kafka", "claimed"), ("Go", "not_demonstrated"))],
    "skipped_strong_evidence": []}


async def test_module_d_formula_usage_model_and_temperature():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return anth(json.dumps(QS), usage=(100, 50))

    c, _ = make(handler)
    out = await generate_interview_questions(REQS, llm=c)
    b = seen[0]
    assert b["max_tokens"] == min(cfg("interview.max_tokens_cap"), 400 + 250 * 2) == 900
    assert b["temperature"] == cfg("interview.temperature")
    assert b["model"] == cfg("interview.model")
    assert out["_usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert out["_model"] == cfg("interview.model") and out["_attempts"] == 1


async def test_module_d_prefers_anthropic_when_key_set_from_mock_client():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return anth(json.dumps(QS))

    base = LLMClient("mock", transport=httpx.MockTransport(handler))
    out = await generate_interview_questions(REQS, llm=base)
    assert seen and out["_model"] == cfg("interview.model")


async def test_module_d_mock_default_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    out = await generate_interview_questions(REQS, llm=LLMClient("mock"))
    assert out["_model"] == "mock-heuristic-v1" and out["_usage"] == {}


async def test_module_d_max_tokens_doubles_and_is_capped():
    budgets = []

    def handler(req):
        budgets.append(json.loads(req.content)["max_tokens"])
        if len(budgets) == 1:  # truncated, unparseable
            return anth('{"interview_questions": [{"skill": "Ka', stop="max_tokens")
        return anth(json.dumps(QS))

    c, _ = make(handler)
    out = await generate_interview_questions(REQS, llm=c)
    assert budgets == [900, 1800] and out["_attempts"] == 2 and out["interview_questions"]

    big = [{"skill": f"S{i}", "evidence_level": "claimed", "jd_priority": "must_have"}
           for i in range(20)]
    seen = []

    def h2(req):
        seen.append(json.loads(req.content)["max_tokens"])
        return anth("{", stop="max_tokens")

    c2, _ = make(h2)
    out2 = await generate_interview_questions(big, llm=c2)
    assert seen == [4000, 4000, 4000] and out2["interview_questions"] == []


async def test_module_d_max_tokens_stop_with_parseable_body_also_retries():
    budgets = []

    def handler(req):
        budgets.append(json.loads(req.content)["max_tokens"])
        return anth(json.dumps(QS), stop="max_tokens" if len(budgets) == 1 else "end_turn")

    c, _ = make(handler)
    out = await generate_interview_questions(REQS, llm=c)
    assert budgets == [900, 1800] and out["_attempts"] == 2


async def test_ollama_generation_is_always_capped():
    """Regression: callers that pass no max_tokens (narrative, integrity interpreter, claim
    judge) sent Ollama no num_predict, so a degenerate JSON generation ran for 22 minutes
    and blocked the GPU queue. Every Ollama request must carry a cap."""
    import json as _json

    import httpx

    from app.llm.client import DEFAULT_MAX_TOKENS, LLMClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"summary": "ok"}'},
                                         "done_reason": "stop"})

    llm = LLMClient("ollama", transport=httpx.MockTransport(handler))
    await llm.complete_json("sys", {"a": 1}, task="summary")
    assert seen["options"]["num_predict"] == DEFAULT_MAX_TOKENS
