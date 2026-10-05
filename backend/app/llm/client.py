"""Provider abstraction: mock | ollama | anthropic, with fallback chain.

The mock provider returns deterministic, schema-valid text computed from simple
heuristics, so the whole platform runs with no API key and no Ollama.
Assumption A-3: the LLM only ever writes narrative prose here. Every judgment,
score, band and penalty is computed in Python (Spec 2.8), so mock mode changes
the wording of a report, never its numbers.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import cfg
from .json_output import STRICT_SUFFIX, extract_json

DEFAULT_MAX_TOKENS = 2000
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OLLAMA_SEED = 7


@dataclass
class LLMResult:
    data: dict
    provider: str
    model: str
    latency_ms: int
    attempts: int = 1
    usage: dict = field(default_factory=dict)
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)  # sanitized failures of earlier providers


class LLMTruncated(RuntimeError):
    """Output hit the token budget and could not be parsed (carries what the caller needs)."""

    def __init__(self, provider: str, model: str, usage: dict, attempts: int) -> None:
        super().__init__(f"{provider}: output truncated at max_tokens")
        self.provider, self.model, self.usage, self.attempts = provider, model, usage, attempts


class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float = 0.0) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


RETRY_AFTER_CAP_S = 20.0  # honour a server's Retry-After, but never stall a screening longer


def _retry_after(resp: Any) -> float:
    try:
        return min(RETRY_AFTER_CAP_S, max(0.0, float(resp.headers.get("retry-after", 0))))
    except (TypeError, ValueError):
        return 0.0  # an HTTP-date or junk value: fall back to normal backoff


@dataclass
class _Raw:
    text: str
    usage: dict
    stop_reason: str | None
    served_model: str | None = None  # what actually answered (OpenRouter may route elsewhere)


def _redact(text: str) -> str:
    """Never let any provider API key reach a log line, exception message or result."""
    for env in (cfg("llm.anthropic.api_key_env", "ANTHROPIC_API_KEY"),
                cfg("llm.openrouter.api_key_env", "OPENROUTER_API_KEY")):
        key = os.getenv(str(env), "")
        if key:
            text = text.replace(key, "[REDACTED]")
    return text


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)  # looked up at call time so tests can patch asyncio.sleep


class LLMClient:
    def __init__(
        self,
        provider: str | None = None,
        *,
        transport: Any = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        """transport: optional httpx transport (tests inject httpx.MockTransport).
        sleep: optional async sleep used for backoff (tests inject a recorder)."""
        self.provider = provider or cfg("llm.provider", "mock")
        self.chain = [self.provider] + [
            p for p in cfg("llm.fallback_chain", ["mock"]) if p != self.provider
        ]
        if "mock" not in self.chain:
            self.chain.append("mock")  # mock is ALWAYS the last resort
        self.calls = 0
        self.prompts: list[dict] = []  # captured for tests; never persisted (privacy)
        self._transport = transport
        self._sleep = sleep or _default_sleep

    @property
    def mock_mode(self) -> bool:
        return self.active == "mock"

    active = "mock"

    async def complete_json(
        self,
        system: str,
        user: Any,
        *,
        task: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        raise_on_truncation: bool = False,
    ) -> LLMResult:
        """model overrides the Anthropic model only. With raise_on_truncation, an output cut off
        by max_tokens raises LLMTruncated instead of falling through, so the caller can retry
        with a bigger budget. The system prompt and payload are sent unmodified (the only
        change ever made is the STRICT suffix on a parse-failure retry)."""
        started = time.perf_counter()
        payload = user if isinstance(user, str) else json.dumps(user, indent=2, sort_keys=True)
        self.prompts.append({"task": task, "system": system, "user": payload})
        errors: list[str] = []
        for provider in self.chain:
            try:
                data, attempts, usage, stop, served = await self._dispatch(
                    provider, system, payload, task, temperature, max_tokens, model
                )
                self.calls += 1
                self.active = provider
                return LLMResult(
                    data=data,
                    provider=provider,
                    model=served or _model_for(provider, model),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    attempts=attempts,
                    usage=usage,
                    stop_reason=stop,
                    errors=errors,
                )
            except LLMTruncated as exc:
                if raise_on_truncation:
                    raise
                errors.append(f"{provider}: {_redact(str(exc))}")
            except Exception as exc:
                errors.append(f"{provider}: {_redact(str(exc))}")
        raise RuntimeError(f"all providers failed: {'; '.join(errors)}")

    async def _dispatch(
        self,
        provider: str,
        system: str,
        user: str,
        task: str,
        temperature: float | None,
        max_tokens: int | None,
        model: str | None,
    ) -> tuple[dict, int, dict, str | None, str | None]:
        if provider == "mock":
            data = MOCK_TASKS[task](json.loads(user) if user.startswith("{") else {"text": user})
            return data, 1, {}, None, None
        if provider == "ollama":
            return await self._http_json(provider, system, user, temperature, max_tokens, model)
        if provider in ("anthropic", "openrouter"):
            if not os.getenv(str(cfg(f"llm.{provider}.api_key_env", ""))):
                raise RuntimeError(f"{provider}: no API key in environment")
            return await self._http_json(provider, system, user, temperature, max_tokens, model)
        raise RuntimeError(f"unknown provider {provider}")

    async def _http_json(
        self,
        provider: str,
        system: str,
        user: str,
        temperature: float | None,
        max_tokens: int | None,
        model: str | None,
    ) -> tuple[dict, int, dict, str | None, str | None]:
        import httpx  # imported lazily: mock mode needs no HTTP stack

        retries = int(cfg("llm.max_retries", 2))
        timeout = float(cfg("llm.timeout_s", 60))
        temp = cfg("llm.temperature", 0) if temperature is None else temperature
        msg = user
        last = "no attempt made"
        backoffs = 0
        usage: dict = {}
        for attempt in range(1, retries + 2):
            try:
                async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as http:
                    raw = await self._request(
                        http, provider, system, msg, temp, max_tokens, model, httpx
                    )
                usage = raw.usage
                try:
                    return extract_json(raw.text), attempt, raw.usage, raw.stop_reason, raw.served_model
                except ValueError as exc:  # JSONDecodeError is a ValueError
                    if raw.stop_reason == "max_tokens":
                        raise LLMTruncated(
                            provider, _model_for(provider, model), usage, attempt
                        ) from None
                    last = f"unparseable JSON ({exc.__class__.__name__})"
                    msg = f"{user}\n\n{STRICT_SUFFIX}"  # parse retry: no backoff needed
            except _Retryable as exc:
                last = str(exc)
                if attempt <= retries:
                    # 1s, 2s, or longer when the server asked for it (free tiers throttle in
                    # bursts; a 1-2s retry measured 3/3 failures against OpenRouter).
                    await self._sleep(max(1 * (2**backoffs), exc.retry_after))
                    backoffs += 1
        raise RuntimeError(_redact(f"{provider} failed after {retries + 1} attempts: {last}"))

    async def _request(
        self, http: Any, provider: str, system: str, msg: str, temp: Any,
        max_tokens: int | None, model: str | None, httpx: Any,
    ) -> _Raw:
        try:
            if provider == "ollama":
                # Always cap generation. Uncapped, a JSON-mode generation that degenerates
                # (endless whitespace) runs until the model's own limit: measured at 22m17s on
                # a local 7B model, long after the client timed out, blocking the single GPU
                # queue for every later call. Anthropic always had a default cap; Ollama did not.
                options: dict[str, Any] = {"temperature": temp, "seed": OLLAMA_SEED,
                                           "num_predict": max_tokens or DEFAULT_MAX_TOKENS}
                resp = await http.post(
                    f"{str(cfg('llm.ollama.base_url')).rstrip('/')}/api/chat",
                    json={
                        "model": cfg("llm.ollama.model"),
                        "stream": False,
                        "format": "json",
                        "options": options,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": msg},
                        ],
                    },
                )
            elif provider == "openrouter":
                # OpenAI-compatible chat completions. The model is always the configured one:
                # Module D's override names a Claude model, which only Anthropic can serve.
                resp = await http.post(
                    f"{str(cfg('llm.openrouter.base_url')).rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {os.environ[str(cfg('llm.openrouter.api_key_env'))]}",
                        "X-Title": "AI Resume Screening Assistant",
                        "content-type": "application/json",
                    },
                    json={
                        "model": cfg("llm.openrouter.model"),
                        # OpenRouter's own routing: if the primary free model is rate-limited
                        # upstream, the next listed one answers. The served model is recorded.
                        "models": [cfg("llm.openrouter.model"), *(cfg("llm.openrouter.fallback_models") or [])],
                        "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
                        "temperature": temp,
                        "seed": OLLAMA_SEED,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": msg},
                        ],
                    },
                )
            else:
                resp = await http.post(
                    ANTHROPIC_URL,
                    headers={
                        "x-api-key": os.environ[str(cfg("llm.anthropic.api_key_env"))],
                        "anthropic-version": ANTHROPIC_VERSION,
                        "content-type": "application/json",
                    },
                    json={
                        "model": model or cfg("llm.anthropic.model"),
                        "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
                        "temperature": temp,
                        "system": system,
                        "messages": [{"role": "user", "content": msg}],
                    },
                )
        except httpx.TimeoutException:
            raise _Retryable("request timed out") from None
        except httpx.ConnectError:
            raise RuntimeError(f"{provider} unreachable") from None  # fall through, no stall
        except httpx.TransportError as exc:
            raise _Retryable(f"transport error {exc.__class__.__name__}") from None
        status = resp.status_code
        if status == 429 or status >= 500:
            raise _Retryable(f"HTTP {status}", _retry_after(resp))
        if status >= 400:
            raise RuntimeError(f"{provider} HTTP {status}")  # 4xx: retrying cannot help
        try:
            body = resp.json()
        except ValueError:
            return _Raw("", {}, None)  # becomes a parse failure and a STRICT retry
        if provider == "openrouter":
            # OpenRouter can report a provider-side failure inside an HTTP 200 body.
            if isinstance(body, dict) and body.get("error"):
                code = (body["error"] or {}).get("code") if isinstance(body["error"], dict) else None
                if code == 429 or (isinstance(code, int) and code >= 500):
                    raise _Retryable(f"openrouter error {code}")
                raise RuntimeError(f"openrouter error {code}")
            choice = (body.get("choices") or [{}])[0]
            u = body.get("usage") or {}
            usage = {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")}
            stop = "max_tokens" if choice.get("finish_reason") == "length" else choice.get("finish_reason")
            return _Raw((choice.get("message") or {}).get("content") or "", usage, stop,
                        body.get("model"))
        if provider == "ollama":
            usage = {
                "input_tokens": body.get("prompt_eval_count"),
                "output_tokens": body.get("eval_count"),
            }
            stop = "max_tokens" if body.get("done_reason") == "length" else body.get("done_reason")
            return _Raw((body.get("message") or {}).get("content", ""), usage, stop)
        text = "".join(
            b.get("text", "") for b in body.get("content") or [] if b.get("type") == "text"
        )
        u = body.get("usage") or {}
        usage = {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens")}
        return _Raw(text, usage, body.get("stop_reason"))


def _model_for(provider: str, override: str | None = None) -> str:
    if provider == "anthropic" and override:
        return override
    return {
        "mock": "mock-heuristic-v1",
        "ollama": str(cfg("llm.ollama.model")),
        "anthropic": str(cfg("llm.anthropic.model")),
        "openrouter": str(cfg("llm.openrouter.model")),
    }[provider]


# --------------------------------------------------------------------------
# Mock provider: deterministic prose built from the structured facts it is given.
# --------------------------------------------------------------------------
def _mock_summary(p: dict) -> dict:
    matched = p.get("matched", [])
    gaps = p.get("missing", [])
    score = p.get("base_score", 0)
    bits = [
        (f"The resume covers {len(matched)} of {p.get('total_requirements', 0)} job requirements, "
         f"giving a base match of {score}.")
    ]
    if matched:
        bits.append("Strongest overlap is in " + ", ".join(matched[:3]) + ".")
    if gaps:
        bits.append("No supporting evidence was found for " + ", ".join(gaps[:3]) + ".")
    bits.append("A recruiter should confirm depth in interview.")
    return {
        "summary": " ".join(bits),
        "strengths": [f"Evidence found for {m}" for m in matched[:4]],
        "risks": [f"No resume evidence for {g}" for g in gaps[:4]]
        or ["No material gaps identified from the resume text alone"],
        "rationale": f"Deterministic policy produced this label from a base score of {score}.",
    }


def _mock_integrity(p: dict) -> dict:
    flags = p.get("flags", [])
    explain = {
        "HIDDEN_TEXT": "The file contains text a reader cannot see on the page.",
        "INJECTION_HIDDEN": "The hidden text contains instructions aimed at an automated screener.",
        "INJECTION_VISIBLE": "The visible text contains instruction-like phrasing.",
        "JD_CLONE": "A long run of wording is copied verbatim from the job description.",
        "METADATA_STUFF": "Technical keywords are packed into file metadata that never renders.",
        "PARSER_DIVERGENCE": "Two PDF readers disagree about what this document contains.",
        "OCR_LAYER": "The file carries a scanned-document text layer, which is normal.",
        "DOCUMENT_COMMENTS": "The file contains reviewer comments that do not print.",
    }
    matters = {
        "HIDDEN_TEXT": "Invisible keywords can lift a keyword-based score a human could not verify.",
        "INJECTION_HIDDEN": "It attempts to make an automated screener ignore its own assessment.",
        "INJECTION_VISIBLE": "It may be ordinary wording, so it carries little weight on its own.",
        "JD_CLONE": "Copied wording inflates similarity without evidencing experience.",
        "METADATA_STUFF": "Hidden keyword fields can skew keyword matching.",
        "PARSER_DIVERGENCE": "Content sitting in one parser's blind spot may go unreviewed.",
        "OCR_LAYER": "It does not affect scoring in any way.",
        "DOCUMENT_COMMENTS": "It does not affect scoring; comment text was excluded from scoring.",
    }
    benign = {
        "HIDDEN_TEXT": "Some templates leave white placeholder text behind.",
        "INJECTION_HIDDEN": "None - this has no legitimate use.",
        "INJECTION_VISIBLE": "Ordinary phrasing in a summary line.",
        "JD_CLONE": "A candidate may mirror the job wording deliberately and openly.",
        "METADATA_STUFF": "Resume builders sometimes write keyword metadata automatically.",
        "PARSER_DIVERGENCE": "Unusual fonts or layouts can confuse one parser.",
        "OCR_LAYER": "This is a scanned document; entirely normal.",
        "DOCUMENT_COMMENTS": "Leftover review comments are common and harmless.",
    }
    verdict = p.get("verdict", "clean")
    headline = {
        "clean": "No integrity concerns were found in this document.",
        "suspicious": "This document contains formatting that could distort an automated score.",
        "attack": "This file contains hidden text aimed at manipulating an automated screener.",
    }[verdict]
    return {
        "headline": headline,
        "intent": "benign",
        "intent_reasoning": "Placeholder; the orchestrator overrides intent deterministically.",
        "manipulation_attempted": False,
        "findings": [
            {
                "code": f["code"],
                "plain_explanation": explain.get(f["code"], "An unusual pattern was found."),
                "why_it_matters": matters.get(f["code"], "It may affect automated scoring."),
                "benign_alternative": benign.get(f["code"], "None - this has no legitimate use."),
            }
            for f in flags
        ],
        "score_adjustment": {
            "base_score": p.get("base_score", 0),
            "penalty": p.get("penalty", 1.0),
            "adjusted_score": 0,
            "explanation": "The scanner's fixed penalty was applied to the base score.",
        },
        "recommended_action": "proceed",
        "audit_log_entry": (
            f"Integrity scan returned verdict '{verdict}' with "
            f"{len(flags)} finding(s); penalty {p.get('penalty', 1.0)} applied."
        ),
    }


_Q = {
    "claimed": (
        ("You list {skill} on your resume - walk me through the most recent thing you built "
         "with it and one decision you had to make along the way."),
        "Distinguishes hands-on use from a skills-list entry.",
        "Depth of {skill} experience stays unverified before an offer.",
    ),
    "not_demonstrated": (
        ("We use {skill} on this team and it is not on your resume - have you run into it in "
         "coursework, a side project or informally, and how would you get up to speed?"),
        "Checks for unlisted exposure and gauges ramp-up speed.",
        "A real skill gap could go unnoticed until the candidate is on the team.",
    ),
    "transferable": (
        ("You have adjacent experience here - where do you expect that to carry over to {skill}, "
         "and where do you expect it to break down?"),
        "Separates genuine transferable understanding from keyword overlap.",
        "Transferability is assumed rather than tested.",
    ),
}


def _mock_questions(p: dict) -> dict:
    reqs = p.get("requirements", [])
    order = {"must_have": 0, "nice_to_have": 1}
    reqs = sorted(reqs, key=lambda r: (order.get(r.get("jd_priority"), 1), r["skill"]))
    out = []
    for r in reqs:
        q, purpose, risk = _Q[r["evidence_level"]]
        out.append(
            {
                "skill": r["skill"],
                "evidence_level": r["evidence_level"],
                "question": q.format(skill=r["skill"]),
                "purpose": purpose.format(skill=r["skill"]),
                "risk_if_unanswered": risk.format(skill=r["skill"]),
            }
        )
    return {"interview_questions": out, "skipped_strong_evidence": []}


def _mock_claim_judge(p: dict) -> dict:
    """Mock mode never changes a deterministic status (numbers stay identical)."""
    d = p.get("deterministic", {})
    return {"status": d.get("status", "UNSUPPORTED"), "evidence": d.get("evidence", []),
            "rationale": d.get("rationale", ""), "judge_confidence": d.get("judge_confidence", 0.0)}


MOCK_TASKS: dict[str, Callable[[dict], dict]] = {
    "summary": _mock_summary,
    "integrity_interpret": _mock_integrity,
    "interview_questions": _mock_questions,
    "claim_judge": _mock_claim_judge,
}
