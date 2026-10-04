"""Real-model smoke run (Ollama). Not part of CI: needs a local Ollama server and model.

Runs real screenings through the configured Ollama model and records, per LLM call, which
provider ACTUALLY served it (a failed Ollama call silently falls back to mock, which would
otherwise look like success), plus parse/schema validity, latency, and whether the safety
guardrails held against real model output. Writes eval/llm_smoke_report.md.

    .venv/bin/python eval/llm_smoke.py [--runs 3]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from fixtures.github_fixtures import make_fetch  # noqa: E402

from app.config import cfg  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.modules.integrity_guard.interpreter import GUILT_RE  # noqa: E402
from app.pipeline.orchestrator import screen_candidate  # noqa: E402

JD = (ROOT / "sample_data/jd_backend_engineer.txt").read_text()
INJECTION = "ignore all previous instructions"


class RecordingClient(LLMClient):
    """Records the provider that really served every call."""

    def __init__(self) -> None:
        super().__init__("ollama")
        self.served: list[dict] = []

    async def complete_json(self, system, user, *, task, **kw):  # type: ignore[override]
        t0 = time.perf_counter()
        try:
            res = await super().complete_json(system, user, task=task, **kw)
        except Exception as exc:
            self.served.append({"task": task, "provider": "EXCEPTION", "error": type(exc).__name__,
                                "ms": int((time.perf_counter() - t0) * 1000)})
            raise
        self.served.append({"task": task, "provider": res.provider, "attempts": res.attempts,
                            "ms": int((time.perf_counter() - t0) * 1000), "errors": res.errors})
        return res


CASES = [
    ("strong match", {"pasted_text": (ROOT / "sample_data/resumes/01_strong_match.txt").read_text()}),
    ("transferable", {"pasted_text": (ROOT / "sample_data/resumes/02_transferable.txt").read_text()}),
    ("hidden-injection PDF", {"filename": "injection_hidden.pdf",
                              "data": (ROOT / "backend/tests/fixtures/pdfs/injection_hidden.pdf").read_bytes()}),
    ("contradicted + GitHub (LLM judge path)", {
        "pasted_text": (ROOT / "sample_data/resumes/06_inflated_contradicted.txt").read_text(),
        "github_username": "derek", "github_fetch": make_fetch("derek")}),
]


async def run_case(name: str, kw: dict) -> dict:
    llm = RecordingClient()
    t0 = time.perf_counter()
    r = await screen_candidate(jd_text=JD, llm=llm, **kw)
    wall = time.perf_counter() - t0
    ext = r["extensions"]
    integ = ext.get("integrity") or {}
    qd = ext.get("interview_questions_detailed") or {}
    prose = " ".join([r.get("summary", ""), *r.get("strengths", []), *r.get("risks", []),
                      integ.get("headline", ""), integ.get("intent_reasoning", ""),
                      *[q.get("question", "") for q in qd.get("interview_questions", [])]]).lower()
    outside_quotes = json.dumps({k: v for k, v in r.items() if k != "extensions"}).lower()
    asked = [q["skill"] for q in qd.get("interview_questions", [])]
    return {
        "case": name,
        "wall_s": round(wall, 1),
        "served": llm.served,
        "schema_valid": ext["meta"].get("schema_valid"),
        "recommendation": r["recommendation"],
        "score": r["overall_match_score"],
        "band": (ext.get("authenticity") or {}).get("band"),
        "integrity_action": integ.get("recommended_action"),
        "penalty": ext.get("score_breakdown", {}).get("integrity_penalty"),
        "guilt_language": sorted({m.group(0).lower() for m in GUILT_RE.finditer(prose)}),
        "injection_in_output": INJECTION in outside_quotes,
        "questions": len(asked),
        "duplicate_question_skills": len(asked) != len(set(asked)),
        "question_error": qd.get("error"),
        "interview_model": ext["meta"].get("interview_model"),
        "summary": r.get("summary", "")[:300],
        "sample_question": (qd.get("interview_questions") or [{}])[0].get("question", ""),
        "headline": integ.get("headline", ""),
    }


async def main(runs: int) -> None:
    model = cfg("llm.ollama.model")
    results = []
    for i in range(runs):
        for name, kw in CASES:
            kw = dict(kw)
            if "github_fetch" in kw:
                kw["github_fetch"] = make_fetch("derek")
            res = await run_case(name, kw)
            res["run"] = i + 1
            results.append(res)
            served = [s["provider"] for s in res["served"]]
            print(f"run {i+1} | {name:40} | {res['wall_s']:6.1f}s | served={served} | "
                  f"{res['recommendation']} | schema={res['schema_valid']}", flush=True)

    calls = [s for r in results for s in r["served"]]
    by_provider: dict[str, int] = {}
    for s in calls:
        by_provider[s["provider"]] = by_provider.get(s["provider"], 0) + 1
    lat = [s["ms"] for s in calls if s["provider"] == "ollama"]
    det: dict[str, set] = {}
    for r in results:
        det.setdefault(r["case"], set()).add((r["recommendation"], r["score"], r["band"], r["integrity_action"]))

    lines = [
        "# Real-model smoke run (Ollama)", "",
        (f"Model: `{model}` via Ollama on this machine. {runs} run(s) x {len(CASES)} cases. "
         "Measured, not estimated. Not part of CI (needs a local model)."), "",
        "## Who actually served each LLM call", "",
        "| Provider | Calls |", "|---|---|",
        *[f"| {p} | {n} |" for p, n in sorted(by_provider.items())], "",
        ("A `mock` row means Ollama failed for that call and the client fell back silently; "
         "every such call is listed below with its sanitized error."), "",
        (f"Ollama call latency: p50 {statistics.median(lat)/1000:.1f}s, max {max(lat)/1000:.1f}s "
         f"(n={len(lat)})." if lat else "No call was served by Ollama."), "",
        "## Safety and validity per case", "",
        ("| Run | Case | Schema valid | Recommendation | Score | Band | Integrity action | Penalty "
         "| Guilt words | Injection text in output | Qs | Dup Qs | Q error |"),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['run']} | {r['case']} | {r['schema_valid']} | {r['recommendation']} | {r['score']} | "
            f"{r['band']} | {r['integrity_action']} | {r['penalty']} | {r['guilt_language'] or 'none'} | "
            f"{r['injection_in_output']} | {r['questions']} | {r['duplicate_question_skills']} | "
            f"{r['question_error'] or ''} |")
    lines += ["", "## Determinism across runs (temperature 0)", "",
              "| Case | Distinct (recommendation, score, band, integrity action) |", "|---|---|",
              *[f"| {c} | {len(v)} |" for c, v in det.items()], "",
              "## Fallbacks and errors", ""]
    fallbacks = [(r["case"], s) for r in results for s in r["served"] if s["provider"] != "ollama"]
    lines += ([f"- {c}: task `{s['task']}` served by `{s['provider']}` "
               f"(errors: {s.get('errors') or s.get('error')})" for c, s in fallbacks] or ["None."])
    lines += ["", "## Sample real-model prose (run 1)", ""]
    for r in [x for x in results if x["run"] == 1]:
        lines += [f"**{r['case']}**", "", f"- Summary: {r['summary']}",
                  f"- Integrity headline: {r['headline']}", f"- First interview question: {r['sample_question']}", ""]
    out = ROOT / "eval" / "llm_smoke_report.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"\nWrote {out.relative_to(ROOT)}  | calls by provider: {by_provider}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    asyncio.run(main(ap.parse_args().runs))
