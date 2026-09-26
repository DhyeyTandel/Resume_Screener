# Assumptions

Recorded per Section 0.4 of the spec. Each is also commented at its call site.

- **A-1 (from the spec itself).** Appendix A was truncated at `"base_score":`. The
  `score_adjustment` fields plus `recommended_action` and `audit_log_entry` are
  reconstructed from TASK items 4-6. Confirm with the Module A owner. Arithmetic is
  recomputed in Python either way, so a wrong reconstruction cannot change a number.
- **A-2.** `config.py` ships a small YAML-subset parser instead of depending on PyYAML,
  so the platform runs on a bare stdlib + FastAPI install. `config.yaml` must stay within
  that subset: nested maps, single-line inline maps, and inline lists.
- **A-3.** The LLM writes prose only. Every judgment, score, penalty, band and
  recommendation is computed in Python (Spec 2.8). Consequence: switching provider between
  `mock`, `ollama` and `anthropic` changes the *wording* of a report, never its numbers.
  This is what makes the determinism and mock-mode invariants testable.
- **A-4.** `sentence-transformers` is optional. Without it, Module C uses a deterministic
  lexical + graph similarity. The interface and the output schema are identical; only the
  `semantic_match` figure would change if embeddings were installed.
- **A-5.** PyMuPDF and pdfplumber are optional. When neither is present the loader falls
  back to a minimal built-in PDF text reader. Span-level integrity detection (colour, font
  size, bbox, render mode) requires PyMuPDF; without it the scanner still runs but sees no
  span geometry, so hidden-text detection on PDFs degrades to `clean`. DOCX hidden-text
  detection (`w:vanish`, white text, tiny font) works with no extra dependency.
- **A-6 (updated).** Module B now has a real GitHub collector (Stage 2) and a
  deterministic claim-evidence judge (Stage 3) - see `collectors/github.py` and
  `matching.py`. The judge is rule-based, not an LLM: it checks language bytes, manifest
  files and commit authorship against each SKILL/PROJECT claim, which keeps every citation
  traceable to a collected artifact (0% hallucinated evidence by construction) without a
  live model call in the hot path. **Still not built:** the LinkedIn and portfolio
  collectors, and per-commit line-authorship stats (the GitHub API budget here counts
  commits, not diff size, so `bulk_import` detection is approximate). With no source
  collected at all, every claim is still `UNVERIFIABLE` -> `INSUFFICIENT_EVIDENCE`, per the
  spec's own rule - never a negative judgment.
- **A-6b (updated).** Stage 4 (consistency) now implements three checks deterministically:
  technology anachronism, overlapping full-time roles (from the resume alone - no second
  source needed), and date-conflict / title-mismatch against a candidate-provided LinkedIn
  export (`consistency.py`, `collectors/linkedin.py`). LinkedIn accepts a `structured_json`
  export reliably; a `pdf_export` is parsed with a best-effort heuristic since LinkedIn's
  PDF layout is not a stable format (noted in the collector's own docstring). **A real bug
  was found and fixed while wiring this in:** `redact_for_scoring`'s phone-number regex was
  loose enough to match a bare `"2019 - 2026"` date range and silently replace it with
  `[CONTACT]`, which would have made Stage 4's overlap check permanently blind on any
  resume that had already been redacted (i.e. every real screening). Fixed with a guard
  that exempts year-range matches from phone redaction; regression tests added in
  `test_redaction.py`.
- **A-6c.** Band priority when both apply: `assessment_confidence < 0.40` -> 
  `INSUFFICIENT_EVIDENCE` is checked *before* the contradiction-forcing rule, matching the
  literal order the spec states them in (Section 11 Stage 6). A contradiction found on a
  candidate with too few sources to reach 0.40 confidence therefore still shows as
  `INSUFFICIENT_EVIDENCE`, not `NEEDS_VERIFICATION` - the contradiction is still visible in
  `contradictions` and `authenticity_flags`, just not promoted to the headline band. This
  is a genuine spec ambiguity; the alternative (contradiction always wins) is one line to
  flip in `engine.py` if the Module B owner prefers it.
- **A-7.** Storage is in-process (dicts) rather than SQLite. The audit log is append-only
  in behaviour but does not survive a restart.
- **A-8.** The frontend is a single dependency-free HTML page served by FastAPI rather than
  a Vite/React/Tailwind app. It covers the layout, tabs, filters and states of Section 14.2.
- **A-9.** JD requirement extraction is heuristic and graph-driven: a requirement is raised
  for any skill in `graph.json` named in the JD, plus years-of-experience and degree lines.
  A skill absent from the graph is not extracted as a requirement.
