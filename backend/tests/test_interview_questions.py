"""Module D behaviour (Spec 12). The client is mocked; no real calls."""
import pytest

from app.llm.client import LLMClient, LLMResult
from app.modules.interview_questions.generator import generate_interview_questions

MIXED = [
    {"skill": "PostgreSQL", "evidence_level": "strong_evidence",
     "detail": "Query optimization bullet", "jd_priority": "must_have"},
    {"skill": "Kafka", "evidence_level": "claimed", "detail": "Listed only",
     "jd_priority": "must_have"},
    {"skill": "Kubernetes", "evidence_level": "not_demonstrated", "detail": None,
     "jd_priority": "nice_to_have"},
    {"skill": "FastAPI", "evidence_level": "transferable", "detail": "Django REST found",
     "jd_priority": "must_have"},
]


class FakeLLM(LLMClient):
    def __init__(self, responses):
        super().__init__("mock")
        self.responses, self.calls_made = list(responses), 0

    async def complete_json(self, system, user, *, task, temperature=None):
        self.calls_made += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResult(data=r, provider="fake", model="fake-1", latency_ms=1)


async def test_all_strong_evidence_makes_no_api_call():
    llm = FakeLLM([])
    out = await generate_interview_questions(
        [{"skill": "Go", "evidence_level": "strong_evidence", "jd_priority": "must_have"}], llm=llm)
    assert llm.calls_made == 0
    assert out["interview_questions"] == []
    assert out["skipped_strong_evidence"] == ["Go"]
    assert out["_attempts"] == 0


async def test_mixed_input_one_question_each_must_have_first():
    out = await generate_interview_questions(MIXED, llm=LLMClient("mock"))
    skills = [q["skill"] for q in out["interview_questions"]]
    assert set(skills) == {"Kafka", "Kubernetes", "FastAPI"}
    assert len(skills) == 3
    assert out["skipped_strong_evidence"] == ["PostgreSQL"]
    assert skills.index("Kubernetes") == 2  # nice_to_have sorts last
    for q in out["interview_questions"]:
        assert q["evidence_level"] != "strong_evidence"
        assert q["question"] and q["purpose"] and q["risk_if_unanswered"]


async def test_invalid_then_valid_gives_two_attempts():
    good = {"interview_questions": [
        {"skill": "Kafka", "evidence_level": "claimed", "question": "q?", "purpose": "p",
         "risk_if_unanswered": "r"}], "skipped_strong_evidence": []}
    llm = FakeLLM([{"garbage": True}, good])
    out = await generate_interview_questions(
        [{"skill": "Kafka", "evidence_level": "claimed", "jd_priority": "must_have"}], llm=llm)
    assert out["_attempts"] == 2
    assert out["interview_questions"][0]["skill"] == "Kafka"


async def test_invented_skill_fails_validation_and_retries():
    bad = {"interview_questions": [
        {"skill": "Rust", "evidence_level": "claimed", "question": "q?", "purpose": "p",
         "risk_if_unanswered": "r"}], "skipped_strong_evidence": []}
    llm = FakeLLM([bad, bad, bad])
    out = await generate_interview_questions(
        [{"skill": "Kafka", "evidence_level": "claimed", "jd_priority": "must_have"}], llm=llm)
    assert out["interview_questions"] == []
    assert "error" in out and out["_attempts"] == 3


async def test_every_attempt_fails_returns_fallback_without_raising():
    llm = FakeLLM([RuntimeError("boom")] * 3)
    out = await generate_interview_questions(
        [{"skill": "Kafka", "evidence_level": "claimed", "jd_priority": "must_have"}], llm=llm)
    assert out["interview_questions"] == []
    assert out["skipped_strong_evidence"] == []
    assert "error" in out and "raw_output" in out


def test_json_fence_and_preamble_stripping():
    from app.llm.json_output import extract_json
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go:\n{"a": 1}\nHope that helps!') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json at all")
