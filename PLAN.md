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
| M4-M5 Module B | **partial**: Stages 1, 5, 6, 7 done; collectors + LLM judge not built (A-6) |
| M6 Module D + adapter + orchestrator + unified report + invariants | **done** |
| M7 Frontend | **done** as a single-file dashboard (A-8) |
| M8 `perturb.py` + eval harness + `report.md` | **not built** |
| M9 README, Docker, demo script | README done; Docker not built |

## Risks

1. **Module B is the headline module (Shane's) and is the least complete.** Its current
   output is honest but uninformative: everything is UNVERIFIABLE. The GitHub collector is
   the highest-value next piece of work, because it is what turns `INSUFFICIENT_EVIDENCE`
   into a real signal.
2. **No eval harness yet**, so none of the Section 16.2 targets have measured values. No
   threshold in `config.yaml` has been tuned against data; they are the spec's defaults.
3. **PDF integrity detection is dependency-gated** (A-5). Install PyMuPDF before demoing
   the hidden-text scenario against a real PDF; the DOCX path and the structural fixtures
   work without it.

## Next, in order

1. `pip install PyMuPDF pdfplumber`, then add PDF red-team fixtures generated as real files.
2. Module B Stage 2 GitHub collector + Stage 3 judge with recorded HTTP cassettes.
3. `eval/perturb.py` (P1-P6) and `eval/run_eval.py`; only then tune thresholds, on a
   held-out split.
4. SQLite persistence for screenings, candidates and the audit log.
