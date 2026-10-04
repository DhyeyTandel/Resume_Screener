"""Live stage progress: the on_stage hook in the pipeline and `progress` in the screening API."""
import asyncio
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.routes as routes
import app.pipeline.orchestrator as orch
from app.llm.client import LLMClient
from app.main import app
from app.pipeline.orchestrator import UI_STAGES, screen_candidate

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
JD = (SAMPLES / "jd_backend_engineer.txt").read_text()
STRONG = (SAMPLES / "resumes/01_strong_match.txt").read_text()


async def _run(events: list, **kw) -> dict:
    kw.setdefault("pasted_text", STRONG)
    return await screen_candidate(
        jd_text=JD, llm=LLMClient("mock"), on_stage=lambda s, st: events.append((s, st)), **kw
    )


def _last(events: list, stage: str) -> str:
    return [st for s, st in events if s == stage][-1]


async def test_stage_order_parse_first_questions_last():
    events: list = []
    await _run(events)
    assert events[0] == ("Parse", "started")
    assert events[-1] == ("Questions", "ok")
    assert {s for s, _ in events} == set(UI_STAGES)  # exactly the six user-facing names
    for stage in UI_STAGES:
        assert _last(events, stage) == "ok"
        # every stage starts before it finishes
        names = [(s, st) for s, st in events if s == stage]
        assert names[0] == (stage, "started")
    first = {s: events.index((s, "started")) for s in UI_STAGES}
    assert first["Parse"] < first["Integrity"] < first["Match"] < first["Skills"]
    assert first["Skills"] < first["Authenticity"] < first["Questions"]


async def test_stage_failure_reports_error_and_pipeline_still_returns(monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("authenticity broke")

    monkeypatch.setattr(orch, "assess", broken)
    events: list = []
    report = await _run(events)
    assert _last(events, "Authenticity") == "error"
    assert _last(events, "Questions") == "ok"
    assert report["extensions"]["meta"]["stages"]["authenticity"]["status"] == "error"


async def test_interpreter_failure_marks_integrity_error(monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("interpreter down")

    monkeypatch.setattr(orch, "interpret", broken)
    events: list = []
    await _run(events)
    assert _last(events, "Integrity") == "error"
    assert _last(events, "Questions") == "ok"


async def test_parse_failure_skips_the_rest():
    events: list = []
    report = await _run(events, pasted_text="   ")
    assert report["extensions"]["status"] == "Error"
    assert _last(events, "Parse") == "error"
    for stage in UI_STAGES[1:]:
        assert _last(events, stage) == "skipped"


async def test_core_failure_marks_match_error_and_skips_later_stages(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("core broke")

    monkeypatch.setattr(orch, "extract_requirements", boom)
    events: list = []
    await _run(events)
    assert _last(events, "Parse") == "ok"
    assert _last(events, "Integrity") == "ok"
    assert _last(events, "Match") == "error"
    for stage in ("Skills", "Authenticity", "Questions"):
        assert _last(events, stage) == "skipped"


async def test_raising_callback_never_breaks_the_pipeline():
    calls: list = []

    def bad(stage, status):
        calls.append((stage, status))
        raise ValueError("callback bug")

    report = await screen_candidate(
        jd_text=JD, pasted_text=STRONG, llm=LLMClient("mock"), on_stage=bad
    )
    baseline = await screen_candidate(jd_text=JD, pasted_text=STRONG, llm=LLMClient("mock"))
    assert calls[0] == ("Parse", "started") and calls[-1] == ("Questions", "ok")
    assert report["recommendation"] == baseline["recommendation"]
    assert report["overall_match_score"] == baseline["overall_match_score"]
    assert report["extensions"]["meta"]["schema_valid"] is True


async def test_on_stage_is_optional():
    report = await screen_candidate(jd_text=JD, pasted_text=STRONG, llm=LLMClient("mock"))
    assert report["extensions"]["meta"]["schema_valid"] is True


# --- API ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENING_DB_PATH", str(tmp_path / "t.db"))
    with TestClient(app) as c:
        yield c


def _wait_complete(client, sid):
    for _ in range(200):
        sc = client.get(f"/v1/screenings/{sid}").json()
        if sc["status"] == "complete":
            return sc
        time.sleep(0.05)
    raise AssertionError("screening did not complete")


def test_progress_appears_while_running_and_stays_consistent_after(client, monkeypatch):
    real = routes.screen_candidate
    reached, release = threading.Event(), threading.Event()

    async def gated(*, on_stage, **kw):
        on_stage("Parse", "started")
        on_stage("Parse", "ok")
        on_stage("Integrity", "started")
        reached.set()
        await asyncio.to_thread(release.wait, 10)
        return await real(on_stage=on_stage, **kw)

    monkeypatch.setattr(routes, "screen_candidate", gated)
    res = client.post("/v1/screenings", data={"jd_text": JD, "pasted_resumes": [STRONG, STRONG + " "]})
    sid = res.json()["screening_id"]
    assert reached.wait(5)

    live = client.get(f"/v1/screenings/{sid}").json()
    assert {"screening_id", "status", "total", "done", "candidates", "progress"} == set(live)
    assert live["status"] == "processing" and live["done"] == 0
    first, second = live["progress"]
    assert first["status"] == "running" and first["current_stage"] == "Integrity"
    assert [s["name"] for s in first["stages"]] == list(UI_STAGES)
    assert [s["status"] for s in first["stages"]][:3] == ["done", "running", "pending"]
    assert second["status"] == "pending" and second["current_stage"] is None
    assert all(s["status"] == "pending" for s in second["stages"])

    release.set()
    final = _wait_complete(client, sid)
    assert final["done"] == 2 == len(final["progress"])
    for p, cand in zip(final["progress"], final["candidates"], strict=True):
        assert p["status"] == "done" and p["current_stage"] is None
        assert [s["status"] for s in p["stages"]] == ["done"] * 6
        assert p["candidate_name"] == cand["candidate_name"]
    # a later read is identical
    assert client.get(f"/v1/screenings/{sid}").json()["progress"] == final["progress"]


def test_progress_marks_failed_candidate_as_error(client):
    res = client.post("/v1/screenings", files=[("files", ("empty.txt", b"  ", "text/plain"))],
                      data={"jd_text": JD})
    final = _wait_complete(client, res.json()["screening_id"])
    p = final["progress"][0]
    assert p["status"] == "error"
    assert p["stages"][0] == {"name": "Parse", "status": "error"}
    assert all(s["status"] == "skipped" for s in p["stages"][1:])
    assert final["candidates"][0]["status"] == "Error"


def test_unknown_screening_has_no_progress_and_404(client):
    assert client.get("/v1/screenings/zzz").status_code == 404
