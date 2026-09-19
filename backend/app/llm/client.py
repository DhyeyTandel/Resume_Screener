"""Provider abstraction: mock | ollama | anthropic, with fallback chain.

The mock provider returns deterministic, schema-valid text computed from simple
heuristics, so the whole platform runs with no API key and no Ollama.
Assumption A-3: the LLM only ever writes narrative prose here. Every judgment,
score, band and penalty is computed in Python (Spec 2.8), so mock mode changes
the wording of a report, never its numbers.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import cfg
from .json_output import STRICT_SUFFIX, extract_json


@dataclass
class LLMResult:
    data: dict
    provider: str
    model: str
    latency_ms: int
    attempts: int = 1
    usage: dict = field(default_factory=dict)


class LLMClient:
    def __init__(self, provider: str | None = None) -> None:
        self.provider = provider or cfg("llm.provider", "mock")
        self.chain = [self.provider] + [
            p for p in cfg("llm.fallback_chain", ["mock"]) if p != self.provider
        ]
        if "mock" not in self.chain:
            self.chain.append("mock")  # mock is ALWAYS the last resort
        self.calls = 0
        self.prompts: list[dict] = []  # captured for tests; never persisted (privacy)

    @property
    def mock_mode(self) -> bool:
        return self.active == "mock"

    active = "mock"

    async def complete_json(
        self, system: str, user: Any, *, task: str, temperature: float | None = None
    ) -> LLMResult:
        started = time.perf_counter()
        payload = user if isinstance(user, str) else json.dumps(user, indent=2, sort_keys=True)
        self.prompts.append({"task": task, "system": system, "user": payload})
        last_err: Exception | None = None
        for provider in self.chain:
            try:
                data = await self._dispatch(provider, system, payload, task, temperature)
                self.calls += 1
                self.active = provider
                return LLMResult(
                    data=data,
                    provider=provider,
                    model=_model_for(provider),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001 - fall through to next provider
                last_err = exc
        raise RuntimeError(f"all providers failed: {last_err}")

    async def _dispatch(
        self, provider: str, system: str, user: str, task: str, temperature: float | None
    ) -> dict:
        if provider == "mock":
            return MOCK_TASKS[task](json.loads(user) if user.startswith("{") else {"text": user})
        if provider == "ollama":
            return await self._http_json(provider, system, user, task, temperature)
        if provider == "anthropic":
            if not os.getenv(cfg("llm.anthropic.api_key_env", "ANTHROPIC_API_KEY")):
                raise RuntimeError("anthropic: no API key in environment")
            return await self._http_json(provider, system, user, task, temperature)
        raise RuntimeError(f"unknown provider {provider}")

    async def _http_json(
        self, provider: str, system: str, user: str, task: str, temperature: float | None
    ) -> dict:
        import httpx  # imported lazily: mock mode needs no HTTP stack

        retries = int(cfg("llm.max_retries", 2))
        timeout = float(cfg("llm.timeout_s", 60))
        temp = cfg("llm.temperature", 0) if temperature is None else temperature
        msg = user
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if provider == "ollama":
                        resp = await client.post(
                            f"{cfg('llm.ollama.base_url')}/api/chat",
                            json={
                                "model": cfg("llm.ollama.model"),
                                "stream": False,
                                "options": {"temperature": temp, "seed": 7},
                                "messages": [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": msg},
                                ],
                            },
                        )
                        resp.raise_for_status()
                        return extract_json(resp.json()["message"]["content"])
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": os.environ[cfg("llm.anthropic.api_key_env")],
                            "anthropic-version": "2023-06-01",
                        },
                        json={
                            "model": cfg("llm.anthropic.model"),
                            "max_tokens": 2000,
                            "temperature": temp,
                            "system": system,
                            "messages": [{"role": "user", "content": msg}],
                        },
                    )
                    resp.raise_for_status()
                    return extract_json(resp.json()["content"][0]["text"])
            except Exception as exc:  # noqa: BLE001
                last = exc
                msg = f"{user}\n\n{STRICT_SUFFIX}"
                if attempt < retries:
                    time.sleep(1 * (attempt + 1))
        raise RuntimeError(f"{provider} failed after retries: {last}")


def _model_for(provider: str) -> str:
    return {
        "mock": "mock-heuristic-v1",
        "ollama": str(cfg("llm.ollama.model")),
        "anthropic": str(cfg("llm.anthropic.model")),
    }[provider]


# --------------------------------------------------------------------------
# Mock provider: deterministic prose built from the structured facts it is given.
# --------------------------------------------------------------------------
def _mock_summary(p: dict) -> dict:
    matched = p.get("matched", [])
    gaps = p.get("missing", [])
    score = p.get("base_score", 0)
    bits = [
        f"The resume covers {len(matched)} of {p.get('total_requirements', 0)} job requirements, "
        f"giving a base match of {score}."
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
    }
    matters = {
        "HIDDEN_TEXT": "Invisible keywords can lift a keyword-based score a human could not verify.",
        "INJECTION_HIDDEN": "It attempts to make an automated screener ignore its own assessment.",
        "INJECTION_VISIBLE": "It may be ordinary wording, so it carries little weight on its own.",
        "JD_CLONE": "Copied wording inflates similarity without evidencing experience.",
        "METADATA_STUFF": "Hidden keyword fields can skew keyword matching.",
        "PARSER_DIVERGENCE": "Content sitting in one parser's blind spot may go unreviewed.",
        "OCR_LAYER": "It does not affect scoring in any way.",
    }
    benign = {
        "HIDDEN_TEXT": "Some templates leave white placeholder text behind.",
        "INJECTION_HIDDEN": "None - this has no legitimate use.",
        "INJECTION_VISIBLE": "Ordinary phrasing in a summary line.",
        "JD_CLONE": "A candidate may mirror the job wording deliberately and openly.",
        "METADATA_STUFF": "Resume builders sometimes write keyword metadata automatically.",
        "PARSER_DIVERGENCE": "Unusual fonts or layouts can confuse one parser.",
        "OCR_LAYER": "This is a scanned document; entirely normal.",
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
        "You list {skill} on your resume - walk me through the most recent thing you built "
        "with it and one decision you had to make along the way.",
        "Distinguishes hands-on use from a skills-list entry.",
        "Depth of {skill} experience stays unverified before an offer.",
    ),
    "not_demonstrated": (
        "We use {skill} on this team and it is not on your resume - have you run into it in "
        "coursework, a side project or informally, and how would you get up to speed?",
        "Checks for unlisted exposure and gauges ramp-up speed.",
        "A real skill gap could go unnoticed until the candidate is on the team.",
    ),
    "transferable": (
        "You have adjacent experience here - where do you expect that to carry over to {skill}, "
        "and where do you expect it to break down?",
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


MOCK_TASKS: dict[str, Callable[[dict], dict]] = {
    "summary": _mock_summary,
    "integrity_interpret": _mock_integrity,
    "interview_questions": _mock_questions,
}
