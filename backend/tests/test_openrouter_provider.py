"""OpenRouter (OpenAI-compatible) provider, exercised only through httpx.MockTransport."""
import json

import httpx
import pytest

from app.llm.client import LLMClient

KEY = "sk-or-v1-test-not-a-real-key"


async def _no_sleep(_s):
    return None


def _client(handler):
    c = LLMClient("openrouter", transport=httpx.MockTransport(handler), sleep=_no_sleep)
    # Pin the chain: the configured fallback includes Ollama, and a real local Ollama server
    # must never be reached from a test.
    c.chain = ["openrouter", "mock"]
    return c


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)


def _ok(content='{"summary": "ok"}', finish="stop"):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7}})


async def test_request_shape_and_parsing():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(req.url), req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return _ok('```json\n{"summary": "fenced"}\n```')

    res = await _client(handler).complete_json("SYS", {"a": 1}, task="summary", temperature=0)
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == f"Bearer {KEY}"
    b = seen["body"]
    assert b["model"] == "nvidia/nemotron-3-super-120b-a12b:free"
    assert b["models"][0] == b["model"] and len(b["models"]) > 1  # OpenRouter fallback list
    assert b["messages"][0] == {"role": "system", "content": "SYS"}
    assert b["response_format"] == {"type": "json_object"}
    assert b["max_tokens"] and "seed" in b
    assert res.data == {"summary": "fenced"} and res.provider == "openrouter"
    assert res.usage == {"input_tokens": 11, "output_tokens": 7}


async def test_module_d_claude_override_is_ignored():
    """Module D passes a Claude model name; OpenRouter must use its configured model."""
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return _ok('{"interview_questions": [], "skipped_strong_evidence": []}')

    await _client(handler).complete_json("s", {}, task="interview_questions", model="claude-sonnet-4-6")
    assert seen["model"] == "nvidia/nemotron-3-super-120b-a12b:free"


async def test_error_inside_a_200_body_is_retried_then_falls_back_to_mock():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"error": {"code": 429, "message": "rate limited " + KEY}})

    res = await _client(handler).complete_json("s", {"matched": [], "missing": []}, task="summary")
    assert len(calls) >= 2  # retried
    assert res.provider == "mock"  # then fell back, never crashed
    assert all(KEY not in e for e in res.errors)


async def test_4xx_falls_through_without_retry_and_never_leaks_the_key():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})

    res = await _client(handler).complete_json("s", {"matched": [], "missing": []}, task="summary")
    assert len(calls) == 1 and res.provider == "mock"
    assert KEY not in repr(res) and all(KEY not in e for e in res.errors)


async def test_truncation_is_reported_as_max_tokens():
    def handler(req):
        return _ok('{"summary": "cut', finish="length")

    from app.llm.client import LLMTruncated
    with pytest.raises(LLMTruncated):
        await _client(handler).complete_json("s", {}, task="summary", raise_on_truncation=True)


async def test_missing_key_falls_back_to_mock(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    c = LLMClient("openrouter")
    c.chain = ["openrouter", "mock"]
    res = await c.complete_json("s", {"matched": [], "missing": []}, task="summary")
    assert res.provider == "mock"


async def test_retry_after_is_honoured_and_capped():
    slept = []

    async def record(s):
        slept.append(s)

    responses = iter([httpx.Response(429, headers={"retry-after": "7"}),
                      httpx.Response(429, headers={"retry-after": "999"}), _ok()])

    c = LLMClient("openrouter", transport=httpx.MockTransport(lambda req: next(responses)), sleep=record)
    c.chain = ["openrouter", "mock"]
    res = await c.complete_json("s", {}, task="summary")
    assert res.provider == "openrouter"
    assert slept == [7.0, 20.0]  # server's value honoured, then capped at 20s



async def test_the_model_that_actually_answered_is_recorded():
    """OpenRouter may route to a fallback model; the result must say which one answered."""
    def handler(req):
        return httpx.Response(200, json={
            "model": "google/gemma-4-31b-it:free",
            "choices": [{"message": {"content": '{"summary": "ok"}'}, "finish_reason": "stop"}]})

    res = await _client(handler).complete_json("s", {}, task="summary")
    assert res.model == "google/gemma-4-31b-it:free"
