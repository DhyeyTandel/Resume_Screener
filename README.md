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
make frontend-install && make frontend-build   # optional: the React dashboard
make dev         # dashboard + API on http://localhost:8077
make docker-up   # same, in Docker (add the ollama profile with make docker-up-ollama)
```

Open <http://localhost:8077>, press **Load sample JD**, paste a resume from
`sample_data/resumes/`, and press **Screen candidates**. API docs are at `/docs`.

## Demo walkthrough (about 5 minutes, no API key needed)

1. `make dev`, open <http://localhost:8077>. The dashboard opens on six precomputed sample
   candidates, labelled as sample data, with the fairness notice and the "Mock analysis
   mode" badge.
2. **Strong match (Priya Raman):** Suggested Shortlist. Open the drawer: the Overview tab
   shows the score breakdown chart and the policy rules that fired.
3. **Transferable (Marcus Ito):** FastAPI and PostgreSQL read *Partially Matched*, not
   Missing. The Skill Intelligence tab shows the supporting skills and the remaining gap.
4. **Hidden-text attack:** score shown as base 100 x 0.40 = 40. The Integrity tab quotes the
   hidden text behind a `hidden text reads:` label, explains each finding in plain language,
   and states that a human decides.
5. **Private work (Alicia Ferreira):** no GitHub, so authenticity reads *Insufficient
   evidence* with no number. It does not block the shortlist.
6. **Contradicted (Derek Voss):** a forked repo with no commits from the candidate plus a
   "12 years of FastAPI" anachronism put authenticity at *Needs verification*, so the
   suggestion is Review Manually, with reasons.
7. **Run your own:** press *Load sample JD*, drop `backend/tests/fixtures/pdfs/injection_hidden.pdf`
   into the upload area, press *Screen candidates*. It is caught as INJECTION_HIDDEN and the
   injected sentence never reaches the summary or scoring.
8. **Audit:** on your own candidate, record a decision in the Audit tab; it is appended to an
   append-only log (the database rejects edits and deletes).

## Running with a real local model

```bash
brew install ollama && ollama serve &
ollama pull qwen2.5:7b-instruct
LLM_PROVIDER=ollama make dev
.venv/bin/python eval/llm_smoke.py --runs 2   # optional: real-model safety/validity report
```

## Swapping the LLM provider

`llm.provider` in `backend/app/config.yaml`, or the `LLM_PROVIDER` env var:

| Provider | Needs | Effect |
|---|---|---|
| `mock` (default) | nothing | Deterministic prose from heuristics. Dashboard shows "Mock analysis mode". |
| `ollama` | Ollama on :11434 | Local model writes the prose. Zero cost. Verified end to end with `qwen2.5:7b-instruct` (`eval/llm_smoke.py`, report in `eval/llm_smoke_report.md`); about 50-100s per candidate on an M4. |
| `anthropic` | `ANTHROPIC_API_KEY` | Best prose quality. |
| `openrouter` | `OPENROUTER_API_KEY` (free tier works) | OpenAI-compatible; defaults to free `nvidia/nemotron-3-super-120b-a12b:free` with free Gemma fallbacks routed by OpenRouter; the model that actually answered is recorded. Measured: ~3s per call, 10-47s per candidate. Free tier is rate-limited (about 50 requests/day; shared free models can be throttled upstream). |

**The provider changes wording, never numbers.** Every score, penalty, band and
recommendation is computed in Python (see ASSUMPTIONS.md A-3).

Env vars: `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `OLLAMA_BASE_URL`, `LLM_PROVIDER`. Put them in a repo-root `.env` (gitignored; loaded automatically; a variable already set in your shell wins). The test suite never reads `.env`, so a real key can never trigger paid calls from `make test`.

**Locking it down:** set `SCREENING_API_KEYS=alice:<long-random-key>` (or `alice:sha256:<hex>` to
store only a hash) to require a key on every `/v1` route except health; the dashboard then asks
for it. Set `SCREENING_CORS_ORIGINS` to your real origins. Rate limits, a bounded screening queue
and a sandboxed PDF parser are on by default (`limits:` and `ingest:` in `config.yaml`).

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

| Module | Owner (Spec 1.1) | Where | State |
|---|---|---|---|
| Core | Teammate 1 | `modules/core_screening/` | built |
| A Integrity Guard | Teammate 2 | `modules/integrity_guard/` | built; tested on real PDF and DOCX files |
| B Authenticity | Shane | `modules/authenticity_engine/` | all 7 stages; GitHub, LinkedIn and portfolio collectors; rule-based judge plus guarded LLM judge |
| C Skill Intelligence | Teammate 3 | `modules/skill_intelligence/` | built |
| D Interview Questions | Teammate 4 | `modules/interview_questions/` | built |
| Policy | shared | `policy/scoring.py`, `policy/recommendation.py` | built - all arithmetic lives here |

## How the safeguards are enforced

Not by prompt wording - by code, with a test for each:

- **Quarantine.** Hidden text never leaves the integrity report. `test_hidden_injection_never_reaches_a_scoring_prompt` asserts on the captured prompts of every other module.
- **Arithmetic in Python.** `enforce_guardrails` discards the LLM's numbers and recomputes `round(base × penalty)`. A deliberately lying LLM response is in the test suite.
- **Missing ≠ negative.** No GitHub gives `INSUFFICIENT_EVIDENCE`, which never blocks a shortlist; `NEEDS_VERIFICATION` does.
- **AI-text indicators carry weight 0.** A buzzword-stuffed rewrite produces an identical score.
- **Protected attributes** are stripped by `redact_for_scoring` before any scoring call; an identity-swap test asserts identical scores.
- **Transferable is never "Missing".** Django/Flask/MySQL against a FastAPI/PostgreSQL JD gives *Partially Matched*, not *Missing*.

## Sample scenarios

`sample_output/` holds committed reports, regenerated by `make seed`.

| Scenario | Result | Why |
|---|---|---|
| `01_strong_match` | Shortlist, 100 | every must-have evidenced |
| `02_transferable` | Review Manually, 73 | FastAPI/PostgreSQL Strongly Transferable, not Missing |
| `05_private_work` | Shortlist, 88 | no GitHub given -> INSUFFICIENT_EVIDENCE, never blocks shortlist |
| `06_inflated_contradicted` | Review Manually, band NEEDS_VERIFICATION | a fork with 0 candidate commits + a "12 years of FastAPI" anachronism, once GitHub evidence clears the 0.40 confidence floor |
| `07_brief` | Review Manually, 100 | caught on confidence 0.24, not on score |
| `03_hidden_text_attack` | Review Manually, base 100 × 0.40 = 40 | both numbers shown |

## Evaluation

`make eval` runs `eval/run_eval.py` against real code (Module A scanner, Module C
transferability, Module D generation, the full pipeline, and 5 of the 6 spec
perturbations) and writes `eval/report.md` from measured values only. Numbers that would
need a real hand-labeled dataset - claim-extraction F1, AUROC, calibration ECE, the
fairness false-flag gap - are listed as **not evaluated**, with the reason, rather than
estimated (Spec 0.5 forbids fabricating results).

## Not built

See PLAN.md and ASSUMPTIONS.md. In short: Module B's Stage 3 judge is deterministic
rule-based rather than an LLM judge, a real annotated dataset for Module B's harder
metrics. CI runs backend tests, lint, eval, 29 frontend component tests, 5 Playwright
end-to-end tests against the real backend, and a Docker build with a health check.
