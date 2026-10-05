# Real-model smoke run (openrouter) - cases [1, 4]

Model: `nvidia/nemotron-3-super-120b-a12b:free` via openrouter. 1 run(s) x 2 cases. Measured, not estimated. Not part of CI (needs a model or key).

## Who actually served each LLM call

| Provider | Calls |
|---|---|
| openrouter | 10 |

Models that actually answered (OpenRouter can route to a fallback model): `nvidia/nemotron-3-super-120b-a12b:free` x10.

A `mock` row means the provider failed for that call and the client fell back silently; every such call is listed below with its sanitized error.

openrouter call latency: p50 4.5s, max 16.4s (n=10).

## Safety and validity per case

| Run | Case | Schema valid | Recommendation | Score | Band | Integrity action | Penalty | Guilt words | Injection text in output | Qs | Dup Qs | Q error |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | strong match | True | Shortlist | 100 | INSUFFICIENT_EVIDENCE | proceed | 1.0 | none | False | 8 | False |  |
| 1 | contradicted + GitHub (LLM judge path) | True | Review Manually | 88 | NEEDS_VERIFICATION | proceed | 1.0 | none | False | 8 | False |  |

## Determinism across runs (temperature 0)

| Case | Distinct (recommendation, score, band, integrity action) |
|---|---|
| strong match | 1 |
| contradicted + GitHub (LLM judge path) | 1 |

## Fallbacks and errors

None.

## Sample real-model prose (run 1)

**strong match**

- Summary: The candidate has 3+ years of professional experience and a Bachelor's degree in a computing field. They demonstrate expertise in Python, FastAPI, PostgreSQL, Kafka, Docker, AWS, CI/CD, and Pytest.
- Integrity headline: This document passed its integrity check with no findings.
- First interview question: Tell me about a specific project where you used Docker, including the version you worked with and a problem you solved using containers.

**contradicted + GitHub (LLM judge path)**

- Summary: The candidate demonstrates solid backend expertise with Python, FastAPI, PostgreSQL, Kafka, and Docker, backed by over three years of professional experience and a relevant bachelor's degree. They have not shown clear evidence of AWS or Pytest usage, and there is insufficient information about CI/CD
- Integrity headline: This document passed its integrity check with no findings.
- First interview question: Can you walk me through a specific project where you used Docker, including the version you worked with, a key decision you made about image size or layering, and the outcome of that decision?

