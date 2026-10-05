"""Concurrency of the screening pipeline. Everything is faked: no network, no real model.

The fake LLM awaits asyncio.sleep(d) per call and then answers like the mock provider. Running
it behind a lock serialises every call, which reproduces the old fully sequential wall time and
gives a baseline whose report must equal the concurrent one."""
import asyncio
import copy
import json
import time
from pathlib import Path

from app.llm.client import LLMClient
from app.modules.authenticity_engine import judge as judge_mod
from app.modules.authenticity_engine.judge import llm_judge_claims
from app.pipeline.orchestrator import screen_candidate
from tests.fixtures.github_fixtures import make_fetch
from tests.test_claim_judge import INDEX

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
JD = (SAMPLES / "jd_backend_engineer.txt").read_text()
STRONG = (SAMPLES / "resumes/01_strong_match.txt").read_text()
ATTACK_PDF = (Path(__file__).parent / "fixtures/pdfs/injection_hidden.pdf").read_bytes()
D = 0.2


class SlowLLM(LLMClient):
    """Mock answers after a fixed delay. `serial=True` takes a lock, i.e. the old behaviour."""

    def __init__(self, delay=D, serial=False, fail_tasks=()):
        super().__init__("mock")
        self.provider = "fake"  # non-mock so the claim judge runs
        self.delay, self.fail_tasks = delay, set(fail_tasks)
        self.lock = asyncio.Lock() if serial else None
        self.tasks: list[str] = []
        self.in_flight = self.peak = 0
        self.peak_by_task: dict[str, int] = {}
        self._by_task: dict[str, int] = {}

    async def complete_json(self, system, user, *, task, **kw):
        if self.lock is not None:
            async with self.lock:
                return await self._call(system, user, task, kw)
        return await self._call(system, user, task, kw)

    async def _call(self, system, user, task, kw):
        self.tasks.append(task)
        self.in_flight += 1
        self._by_task[task] = self._by_task.get(task, 0) + 1
        self.peak = max(self.peak, self.in_flight)
        self.peak_by_task[task] = max(self.peak_by_task.get(task, 0), self._by_task[task])
        try:
            await asyncio.sleep(self.delay)
            if task in self.fail_tasks:
                raise RuntimeError("simulated provider failure")
            return await LLMClient.complete_json(self, system, user, task=task, **kw)
        finally:
            self.in_flight -= 1
            self._by_task[task] -= 1


async def _screen(llm, **kw):
    kw.setdefault("pasted_text", STRONG)
    t0 = time.perf_counter()
    r = await screen_candidate(jd_text=JD, llm=llm, candidate_name="Priya Raman",
                               github_username="priya", github_fetch=make_fetch("priya"), **kw)
    return r, time.perf_counter() - t0


def _strip_timing(obj):
    """Drop latency fields and the random candidate id; everything else must match."""
    if isinstance(obj, dict):
        return {k: _strip_timing(v) for k, v in obj.items()
                if k not in ("latency_ms", "candidate_id", "latency_s", "_latency_seconds")}
    if isinstance(obj, list):
        return [_strip_timing(v) for v in obj]
    return obj


def _fingerprint(r):
    return json.dumps(_strip_timing(r), sort_keys=True, default=str)


async def test_concurrent_run_is_faster_than_the_sequential_sum_with_identical_content():
    seq_llm = SlowLLM(serial=True)
    seq, seq_wall = await _screen(seq_llm)
    con_llm = SlowLLM()
    con, con_wall = await _screen(con_llm)
    n = len(seq_llm.tasks)
    assert n >= 4 and "claim_judge" in seq_llm.tasks and "summary" in seq_llm.tasks
    assert sorted(seq_llm.tasks) == sorted(con_llm.tasks)  # same calls, only overlapped
    assert seq_wall >= n * D * 0.95  # the baseline really is the sequential sum
    assert con_wall < seq_wall * 0.7, (con_wall, seq_wall)  # clear margin
    print(f"\nsequential {seq_wall:.2f}s, concurrent {con_wall:.2f}s, {n} calls")
    # identical report content
    for key in ("recommendation", "overall_match_score"):
        assert con[key] == seq[key]
    assert con["extensions"]["score_breakdown"] == seq["extensions"]["score_breakdown"]
    assert con["extensions"]["meta"]["provenance"] == seq["extensions"]["meta"]["provenance"]
    assert [c["status"] for c in con["extensions"]["authenticity"]["claims"]] == \
        [c["status"] for c in seq["extensions"]["authenticity"]["claims"]]
    assert _fingerprint(con) == _fingerprint(seq)
    assert con["extensions"]["meta"]["schema_valid"] is True


async def test_integrity_interpreter_overlaps_authenticity_and_narrative():
    # An attack file makes the scanner raise flags, so the interpreter really calls the model.
    async def go(llm):
        t0 = time.perf_counter()
        r = await screen_candidate(jd_text=JD, filename="injection_hidden.pdf", data=ATTACK_PDF,
                                   llm=llm)
        return r, time.perf_counter() - t0

    seq_llm, con_llm = SlowLLM(serial=True), SlowLLM()
    seq, seq_wall = await go(seq_llm)
    con, con_wall = await go(con_llm)
    assert con_llm.tasks.count("integrity_interpret") == 1 and "summary" in con_llm.tasks
    stages = con["extensions"]["meta"]["stages"]
    assert list(stages)[:3] == ["parse", "integrity", "core"]  # fixed order, whatever finished first
    assert list(stages)[-1] == "interview"
    # interpreter and narrative overlap: one model wait saved (D is 0.2), minus a margin.
    assert con_wall < seq_wall - 0.7 * D, (con_wall, seq_wall)
    assert _fingerprint(con) == _fingerprint(seq)


async def test_same_inputs_give_the_same_report_every_time():
    runs = [(await _screen(SlowLLM(delay=0.02)))[0] for _ in range(3)]
    assert len({_fingerprint(r) for r in runs}) == 1
    attack = [await screen_candidate(jd_text=JD, filename="injection_hidden.pdf", data=ATTACK_PDF,
                                     llm=SlowLLM(delay=0.02)) for _ in range(2)]
    assert _fingerprint(attack[0]) == _fingerprint(attack[1])


async def test_interpreter_failure_still_applies_the_attack_penalty_concurrently():
    llm = SlowLLM(delay=0.02, fail_tasks={"integrity_interpret"})
    r = await screen_candidate(jd_text=JD, filename="injection_hidden.pdf", data=ATTACK_PDF, llm=llm)
    ext = r["extensions"]
    assert "integrity_interpret" in llm.tasks
    assert ext["score_breakdown"]["integrity_penalty"] == 0.40
    assert r["overall_match_score"] == round(ext["score_breakdown"]["base_score"] * 0.40)
    assert ext["integrity"]["recommended_action"] == "disqualify_review"
    assert r["recommendation"] != "Shortlist"
    assert r["summary"]  # the narrative was not lost to the interpreter failure
    assert ext["meta"]["schema_valid"] is True


async def test_stage_events_stay_consistent_when_the_interpreter_finishes_last():
    events: list[tuple[str, str]] = []
    llm = SlowLLM(delay=0.02)
    await screen_candidate(jd_text=JD, filename="injection_hidden.pdf", data=ATTACK_PDF, llm=llm,
                           on_stage=lambda s, st: events.append((s, st)))
    terminal = [(s, st) for s, st in events if st in ("ok", "error", "skipped")]
    assert sorted(s for s, _ in terminal) == sorted(
        ["Parse", "Integrity", "Match", "Skills", "Authenticity", "Questions"])
    assert len(terminal) == 6  # each stage closes exactly once
    assert events.index(("Integrity", "started")) < events.index(("Match", "started"))
    done = {s: i for i, (s, st) in enumerate(events) if st == "ok"}
    assert done["Questions"] == max(done.values())  # Module D is still last


# --- judge ----------------------------------------------------------------------------------

def _claims(n):
    return [{"claim_id": f"C{i}", "type": "SKILL", "text": f"Rust{i}",
             "deterministic": {"status": "UNSUPPORTED", "evidence": [], "rationale": "det",
                               "judge_confidence": 0.5}} for i in range(n)]


class JudgeFake(LLMClient):
    """Answers each claim from its id; later claims finish first (shorter sleep)."""

    def __init__(self, n):
        super().__init__("mock")
        self.provider = "fake"
        self.n, self.in_flight, self.peak = n, 0, 0

    async def complete_json(self, system, user, *, task, **kw):
        from app.llm.client import LLMResult
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            idx = int(user["claim"]["claim_id"][1:])
            await asyncio.sleep(0.02 * (self.n - idx))
            if idx == 3:
                raise RuntimeError("boom")  # an error keeps the deterministic result
            data = {"status": "WEAK", "evidence": [{"citation": "https://priya.dev", "note": f"n{idx}"}],
                    "rationale": f"r{idx}. Second. Third.", "judge_confidence": 7}
            return LLMResult(data=data, provider="fake", model="f", latency_ms=1)
        finally:
            self.in_flight -= 1


async def test_judge_never_exceeds_the_semaphore_and_keeps_claim_order(monkeypatch):
    for limit in (1, 2, 4):
        monkeypatch.setattr(judge_mod, "_judge_concurrency", lambda limit=limit: limit)
        llm = JudgeFake(8)
        out = await llm_judge_claims(_claims(8), INDEX, llm)
        assert llm.peak == min(limit, 8)
        assert [o["claim_id"] for o in out] == [f"C{i}" for i in range(8)]
        for i, o in enumerate(out):
            if i == 3:  # LLM error: deterministic result kept
                assert o["status"] == "UNSUPPORTED" and not o["upgraded"] and o["llm_called"]
            else:
                assert o["status"] == "WEAK" and o["upgraded"]
                assert o["rationale"] == f"r{i}. Second."  # 2 sentence cap
                assert o["judge_confidence"] == 1.0  # clamp
                assert [e["note"] for e in o["evidence"]] == [f"n{i}"]


async def test_judge_results_do_not_depend_on_the_limit(monkeypatch):
    outs = []
    for limit in (1, 3, 16):
        monkeypatch.setattr(judge_mod, "_judge_concurrency", lambda limit=limit: limit)
        outs.append(copy.deepcopy(await llm_judge_claims(_claims(6), INDEX, JudgeFake(6))))
    assert outs[0] == outs[1] == outs[2]


async def test_judge_concurrency_config_default_is_bounded():
    from app.config import cfg
    assert 1 <= judge_mod._judge_concurrency() <= 16
    assert cfg("authenticity.judge_concurrency") == judge_mod._judge_concurrency()
