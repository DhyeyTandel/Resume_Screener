# AI Resume Screening Assistant

Recruiter-facing decision support. It never makes a hiring decision, never outputs
"fraud" or "fake", and never auto-rejects. A human recruiter always decides.

## Run it

Works with **no API key and no Ollama** - the mock provider is always the last resort in
the fallback chain, so the demo cannot die.

**Prerequisites:** Python 3.11+ (CI uses 3.12; also tested on 3.14), Node 22+ and npm for the
dashboard, and `make`. A fresh clone to a working dashboard takes about 3 minutes, mostly
`pip` downloads.

```bash
make install                                  # .venv + pip install -r requirements.txt
make frontend-install && make frontend-build  # builds the React dashboard (needed for the UI)
make dev                                      # dashboard + API on http://localhost:8077
                                              # (make dev PORT=8095 if 8077 is taken)
make test                                     # backend test suite
make seed                                     # re-screen the samples into sample_output/
make eval                                     # write eval/report.md
make docker-build && make docker-up           # same app in Docker
make docker-up-ollama                         # Docker plus an Ollama container
```

Without `make frontend-build`, `/` falls back to a simpler single-file dashboard; the demo
below assumes the React one. Open <http://localhost:8077>, press **Load sample JD**, upload or
paste a resume from `sample_data/resumes/`, and press **Screen candidates**. API docs are at
`/docs`. `make seed` output is deterministic, so it only changes `sample_output/` when a result
actually changes.

## Demo walkthrough (about 5 minutes, no API key needed)

1. `make dev`, open <http://localhost:8077>. The dashboard opens on eight precomputed sample
   candidates (table below), labelled as sample data, with the fairness notice and the
   "Mock analysis mode" badge.
2. **Strong match (Priya Raman):** Suggested Shortlist. Open the drawer: the Overview tab
   shows the score breakdown chart and the policy rules that fired.
3. **Transferable (Marcus Ito):** FastAPI and PostgreSQL read *Partially Matched*, not
   Missing. The Skill Intelligence tab shows the supporting skills and the remaining gap.
4. **Hidden-text attack ("Attack Scenario"):** a real PDF with white-on-white instructions,
   a hidden copy of the job description and keyword-stuffed metadata. Score shown as base
   100 x 0.40 = 40. The Integrity tab quotes the
   hidden text behind a `hidden text reads:` label, explains each finding in plain language,
   and states that a human decides.
5. **Private work (Alicia Ferreira):** no GitHub, so authenticity reads *Insufficient
   evidence* with no number. It does not block the shortlist.
6. **Contradicted (Derek Voss):** his LinkedIn export dates his Lead Engineer role three
   years later than his resume does, so that role is *Contradicted* and authenticity reads
   *Needs verification* (Review Manually, with reasons). His fork claimed as own work and
   the "12 years of FastAPI" anachronism are shown as things to verify, not as proof.
7. **Brief, scanned and AI-polished:** Sam Okafor's two-line resume is Review Manually on
   *low confidence*, never Not Recommended; the scanned resume carries an OCR layer and is
   not penalised; the AI-polished rewrite of Priya's resume scores exactly like the original.
8. **Run your own:** press *Load sample JD*, drop `backend/tests/fixtures/pdfs/injection_hidden.pdf`
   into the upload area, press *Screen candidates*. It is caught as INJECTION_HIDDEN and the
   injected sentence never reaches the summary or scoring.
9. **Audit:** on your own candidate, record a decision in the Audit tab; it is appended to an
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

**The provider changes wording, not the arithmetic.** Every score, penalty, band and
recommendation is computed in Python (ASSUMPTIONS.md A-3). One exception to "never numbers":
with a real model configured, the guarded LLM claim judge (A-12) may upgrade a WEAK or
UNSUPPORTED evidence claim within code-enforced caps, which can move the authenticity rating.
In mock mode it changes nothing.

Env vars: `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OLLAMA_BASE_URL`,
`OLLAMA_MODEL`, `GITHUB_TOKEN`, and for locking it down `SCREENING_API_KEYS`,
`SCREENING_ADMIN_LABELS`, `SCREENING_CORS_ORIGINS` (see `.env.example`). Put them in a repo-root `.env` (gitignored; loaded automatically; a variable already set in your shell wins). The test suite never reads `.env`, so a real key can never trigger paid calls from `make test`.

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
- **AI-text indicators never move a decision.** A buzzword-stuffed or AI-polished truthful rewrite gives an identical score, recommendation and authenticity band. (The *displayed* authenticity number keeps the spec's small inflation term; the band uses an inflation-free score, ASSUMPTIONS.md A-31.)
- **Protected attributes** are masked by `redact_for_scoring` before scoring: the candidate's name (never words from a filename), contact details, inline age/marital status/nationality/pronouns, religious and affinity groups, and institutions (keyword-based plus a curated list). It is pattern-based, not a full NER model, so it is a strong default rather than a guarantee. Identity-swap tests (name, gender, college) assert identical scores.
- **Parsing failures never become adverse labels.** A resume the parser cannot assess gets Review Manually with the reason, never Not Recommended.
- **Transferable is never "Missing".** Django/Flask/MySQL against a FastAPI/PostgreSQL JD gives *Partially Matched*, not *Missing*.

## Sample scenarios

`sample_output/` holds committed reports, regenerated by `make seed`.

| Scenario | Candidate | Result | Why |
|---|---|---|---|
| `01_strong_match` | Priya Raman | Shortlist, 100 | every must-have evidenced |
| `02_transferable` | Marcus Ito | Review Manually, 73 | FastAPI/PostgreSQL Strongly Transferable, not Missing; Kafka missing |
| `03_hidden_text_attack` | Attack Scenario | Review Manually, base 100 × 0.40 = 40 | real PDF: hidden injection, hidden JD copy, metadata stuffing |
| `04_scanned_ocr` | Scanned Resume | Review Manually, low confidence | OCR layer over a page image: benign, no penalty |
| `05_private_work` | Alicia Ferreira | Shortlist, 88 | no GitHub -> Insufficient evidence, which never blocks a shortlist |
| `06_inflated_contradicted` | Derek Voss | Review Manually, Needs verification | LinkedIn contradicts a role's dates; fork claimed as own |
| `07_brief` | Sam Okafor | Review Manually, 100 | low confidence (0.24), not a low score |
| `08_ai_polished_truthful` | Priya Raman (AI-polished copy) | Shortlist, 100 | same facts in AI-style wording: identical to 01 |

## Evaluation

`make eval` runs `eval/run_eval.py` against real code and writes `eval/report.md` from measured
values only: the Module A scanner on real generated PDF and DOCX attack and clean files, Module C
transferability, Module D generation, the full pipeline, all six spec perturbations, and a
300-candidate **synthetic** Module B corpus (`eval/synthetic/`) with bootstrap confidence
intervals. Synthetic numbers find bugs and track regressions but do not establish real-world
performance: the corpus and its scoring share an author. Metrics that need real hand-labelled
resumes are reported as not evaluated rather than estimated. Real-model behaviour is in
`eval/llm_smoke_report*.md` (`eval/llm_smoke.py`).

## Known gaps

See ASSUMPTIONS.md (decisions) and PLAN.md (open items). In short:
- No real labelled dataset; several Spec 16.2 targets are unmet or synthetic-only (P2, P6, AUROC, calibration ECE).
- JD and resume extraction are heuristic and graph-based (no LLM extraction path); requirements outside the skill graph are surfaced as unrecognised rather than scored.
- Module C semantic matching is graph and lexical, not embedding-based.
- The Anthropic provider is tested only against simulated HTTP.
- Rate limits and the screening queue are per process.

CI runs lint, type checks, the backend suite, the eval and seed, frontend component tests,
Playwright end-to-end tests (including an authenticated project) against the real backend, and
a Docker build with a health check.
