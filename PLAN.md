# PLAN

## What was built (a vertical slice of every module)

The spec is a 9-milestone build. This pass takes one end-to-end slice through all of it:
a recruiter can paste a JD and a resume and get back a complete, schema-valid, explainable
report, with the safeguards of Section 2 enforced in code and asserted by tests.

| Milestone | State |
|---|---|
| M0 scaffold, config, LLM client, JSON helper, redaction, vocab | **done** (DB is in-process, see A-7) |
| M1 parsing + Module A scanner + interpreter + red-team tests | **done** (PDF span geometry needs PyMuPDF, A-5) |
| M2 Core + scoring/recommendation policy + `POST /v1/screenings` | **done** |
| M3 Module C graph, semantic matching, transferability | **done** (lexical+graph similarity, A-4) |
| M4-M5 Module B | **done except the Stage-3 LLM judge**: Stages 1, 4, 5, 6, 7 + live GitHub, LinkedIn and portfolio collectors, all deterministic (A-6, A-7) |
| M6 Module D + adapter + orchestrator + unified report + invariants | **done** |
| M7 Frontend | **done** as a single-file dashboard (A-8) |
| M8 `perturb.py` + eval harness + `report.md` | **done** - see eval/report.md, measured not estimated |
| M9 README, Docker, demo script | README done; Docker not built |

## Risks

1. **Module B's Stage 3 judge is deterministic rule-based, not an LLM.** This keeps
   every citation traceable (0% hallucination) but means nuanced claims outside the
   built-in rubric (a claim the rules don't recognize a pattern for) fall through to
   UNVERIFIABLE/UNSUPPORTED rather than getting a judged rationale.
2. **No eval harness yet**, so none of the Section 16.2 targets have measured values. No
   threshold in `config.yaml` has been tuned against data; they are the spec's defaults.
3. **PDF integrity detection is dependency-gated** (A-5). Install PyMuPDF before demoing
   the hidden-text scenario against a real PDF; the DOCX path and the structural fixtures
   work without it.
4. **`bulk_import` detection is approximate** (A-6): it counts commits, not diff size,
   because per-commit stats would multiply the GitHub API call budget per repo.

## Next, in order

1. `pip install PyMuPDF pdfplumber`, then add PDF red-team fixtures generated as real files.
2. Graduation-year consistency check (experience dates vs education, Spec 11 Stage 4)
   - the last unimplemented Stage 4 check named in the spec.
3. Collect a real, consenting-candidate dataset and re-run `eval/run_eval.py`'s "Not
   evaluated here" metrics (claim-extraction F1, AUROC, fairness gap, calibration ECE);
   only then tune thresholds, on a held-out split (never the test set).
4. SQLite persistence for screenings, candidates and the audit log.
