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
- **A-6.** Module B ships Stages 1, 5, 6 and 7 deterministically. The live collectors
  (Stage 2) and the LLM claim-evidence judge (Stage 3) are **not built**. With no collected
  source, every claim is `UNVERIFIABLE`, which by the spec's own rule lowers *confidence*
  and yields `INSUFFICIENT_EVIDENCE` - never a negative judgment. This is the correct
  degraded behaviour, not a placeholder that fabricates verdicts.
- **A-7.** Storage is in-process (dicts) rather than SQLite. The audit log is append-only
  in behaviour but does not survive a restart.
- **A-8.** The frontend is a single dependency-free HTML page served by FastAPI rather than
  a Vite/React/Tailwind app. It covers the layout, tabs, filters and states of Section 14.2.
- **A-9.** JD requirement extraction is heuristic and graph-driven: a requirement is raised
  for any skill in `graph.json` named in the JD, plus years-of-experience and degree lines.
  A skill absent from the graph is not extracted as a requirement.
