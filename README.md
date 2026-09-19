# AI Resume Screening Assistant

Recruiter-facing decision support. It never makes a hiring decision, never outputs
"fraud" or "fake", and never auto-rejects. A human recruiter always decides.

## Run it

Works with **no API key and no Ollama** - the mock provider is always the last resort in
the fallback chain, so the demo cannot die.

```bash
make install     # python3 -m venv .venv && pip install -r requirements.txt
make test        # 44 tests
make seed        # screen every sample resume, write sample_output/
make dev         # dashboard + API on http://localhost:8077
```

Open <http://localhost:8077>, press **Load sample JD**, paste a resume from
`sample_data/resumes/`, and press **Screen candidates**. API docs are at `/docs`.

## Swapping the LLM provider

`llm.provider` in `backend/app/config.yaml`, or the `LLM_PROVIDER` env var:

| Provider | Needs | Effect |
|---|---|---|
| `mock` (default) | nothing | Deterministic prose from heuristics. Dashboard shows "Mock analysis mode". |
| `ollama` | Ollama on :11434 | Local model writes the prose. Zero cost. |
| `anthropic` | `ANTHROPIC_API_KEY` | Best prose quality. |

**The provider changes wording, never numbers.** Every score, penalty, band and
recommendation is computed in Python (see ASSUMPTIONS.md A-3).

Env vars: `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `OLLAMA_BASE_URL`, `LLM_PROVIDER`.

## Architecture

```
JD text ─┐
Resume ──┴─► [0 Parse] ─► [1 Integrity Guard A] ─► visible text only ─┐
                                                                      ▼
                          [2 Core: JD requirements + structuring + matching]
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼ (asyncio.gather)                  ▼
            [3 Skill Intelligence C]            [4 Authenticity B]
                        └─────────────────┬─────────────────┘
                                          ▼
                    [5 Aggregation + recommendation policy (deterministic)]
                                          ▼
                          [6 Interview Questions D] ─► report ─► API ─► dashboard
```

| Module | Where | State |
|---|---|---|
| Core | `modules/core_screening/` | built |
| A Integrity Guard | `modules/integrity_guard/` | built (PDF spans need PyMuPDF) |
| B Authenticity | `modules/authenticity_engine/` | Stages 1/5/6/7 only - no collectors |
| C Skill Intelligence | `modules/skill_intelligence/` | built |
| D Interview Questions | `modules/interview_questions/` | built |
| Policy | `policy/scoring.py`, `policy/recommendation.py` | built - all arithmetic lives here |

## How the safeguards are enforced

Not by prompt wording - by code, with a test for each:

- **Quarantine.** Hidden text never leaves the integrity report. `test_hidden_injection_never_reaches_a_scoring_prompt` asserts on the captured prompts of every other module.
- **Arithmetic in Python.** `enforce_guardrails` discards the LLM's numbers and recomputes `round(base × penalty)`. A deliberately lying LLM response is in the test suite.
- **Missing ≠ negative.** No GitHub gives `INSUFFICIENT_EVIDENCE`, which never blocks a shortlist; `NEEDS_VERIFICATION` does.
- **AI-text indicators carry weight 0.** A buzzword-stuffed rewrite produces an identical score.
- **Protected attributes** are stripped by `redact_for_scoring` before any scoring call; an identity-swap test asserts identical scores.
- **Transferable is never "Missing".** Django/Flask/MySQL against a FastAPI/PostgreSQL JD gives *Partially Matched*, not *Missing*.

## Sample scenarios

`sample_output/` holds committed reports. `01_strong_match` → Shortlist (100).
`02_transferable` → Review Manually (73, FastAPI and PostgreSQL Strongly Transferable).
`07_brief` → Review Manually on confidence 0.24, not on score.
`03_hidden_text_attack` → base 100 × 0.40 penalty = 40, shown as both numbers.

## Not built

See PLAN.md and ASSUMPTIONS.md. In short: Module B's collectors and judge, the eval
harness and `eval/report.md`, SQLite persistence, Docker, and the Vite/React frontend.
**No evaluation numbers are claimed anywhere, because no eval has been run.**
