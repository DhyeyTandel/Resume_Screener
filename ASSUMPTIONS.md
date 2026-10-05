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
- **A-7 (portfolio collector).** `collectors/portfolio.py` fetches the page and a
  dependency-light HTML-text extractor is used when BeautifulSoup is not installed (same
  zero-cost-first pattern as the PDF loader, A-5). robots.txt is honoured with a minimal
  stdlib-only parser that only reads `User-agent: *` rules (no per-agent identification) -
  a conservative simplification: a `*` disallow still blocks the collector, it just won't
  notice a rule aimed at a different, named agent. A dead live-demo link is recorded as the
  `dead_demo_link` flag and is explicitly never added to `contradictions` (Spec 11 Stage 2:
  "dead link = weak evidence only, never a contradiction") - tested directly.
- **A-8 (a real bug caught while building A-6b/A-7, not a hypothetical one).** The first
  version of `check_linkedin_consistency` and `judge_role_claim` paired a resume role with
  the closest-TITLED LinkedIn role regardless of employer. Tested against a live LinkedIn
  export during manual verification, this matched "Backend Engineer" at Corvid Systems
  against "Senior Backend Engineer" at a completely different company (Northwind Payments)
  purely because the titles were similar, and reported a false `date_conflict` between two
  unrelated jobs. Fixed by pairing on employer first (fuzzy match >= 60%) and only then
  comparing title/dates within that pair; a resume role with no matching employer on
  LinkedIn is left uncompared - missing evidence, never a contradiction (Spec 2.4).
  Regression tests in `test_linkedin_consistency.py`. This was found by manually exercising
  the live API with a real upload, not by the unit tests alone - the existing tests all
  happened to use matching companies on both sides, which is exactly the case this bug
  doesn't trigger on.
- **A-7 (storage, updated).** Screenings, candidate reports and the audit log persist in
  SQLite via stdlib `sqlite3` (`backend/app/db/store.py`) rather than SQLAlchemy, to keep
  the dependency list short. The audit log is append-only at the database level (triggers
  reject UPDATE and DELETE), not just by API surface. A screening still "processing" when
  the server restarts is marked `interrupted` on startup, since nothing resumes it; the
  dashboard stops polling and tells the recruiter to re-run.
- **A-8 (frontend, updated).** The primary UI is now `frontend-app/` (Vite, React 18,
  TypeScript, Tailwind, TanStack Query, recharts), served by FastAPI from
  `frontend-app/dist` when built; `frontend/index.html` remains as a no-Node fallback. The
  landing page shows the six precomputed `sample_output/` reports via `GET /v1/samples`,
  flagged as sample data and excluded from the audit form (they are not in the database).
  A styling bug was caught by hand before merge: chip classes were built at runtime
  (`chip-${tone}`), which Tailwind cannot see, so every green/amber/red chip, including
  "Needs verification" and integrity flags, compiled to nothing and rendered as plain
  text. Class names are now spelled out literally.
- **A-9.** JD requirement extraction is heuristic and graph-driven: a requirement is raised
  for any skill in `graph.json` named in the JD, plus years-of-experience and degree lines.
  A skill absent from the graph is not extracted as a requirement.
- **A-9 (real-PDF bugs).** Running the integrity scanner against generated PDF files
  (`backend/tests/fixtures/pdfs/`) exposed two bugs the in-memory test doubles could not:
  (1) PyMuPDF span dicts carry no `render_mode` key, so invisible (render mode 3) text was
  never detected and an OCR layer read as ordinary visible text; fixed via
  `page.get_texttrace()`. (2) PyMuPDF clips extraction to the page box, so off-page text was
  silently dropped and only surfaced as a misleading PARSER_DIVERGENCE; fixed by extracting
  with an unbounded clip so HIDDEN_TEXT fires correctly.
- **A-10 (graduation check).** Stage 4's graduation consistency check flags only a
  senior/lead/principal/staff/manager title starting more than a year before the stated
  graduation year. Working while studying is normal, so ordinary, intern, junior, part-time
  and assistant roles are never flagged. The `role_level` argument is accepted but unused.
- **A-11 (Docker).** Docker was not available on the build machine, so the Dockerfile and
  compose file are syntax-checked (YAML parse) but have never been built here; the first
  real build is CI. The Ollama service is opt-in via `docker compose --profile ollama up`.
- **A-12 (LLM claim judge).** Stage 3 now has an LLM judge layered on the deterministic
  rubric (`authenticity_engine/judge.py`). It can only upgrade WEAK/UNSUPPORTED claims,
  within code-enforced caps, and any citation not in the collected evidence index is
  dropped. VERIFIED additionally requires the cited repo's own metadata to show the claim:
  review before merge found that without this, the model could cite any authored repo
  (for example a Python service, for a Kafka claim) and turn an unsupported claim into a
  verified one. The judge proposes nothing in mock mode, so sample outputs are unchanged.
- **A-13 (real-DOCX bugs).** Running the integrity scanner against generated Word files
  (`backend/tests/fixtures/docx/`, stdlib-only generator, checked to render correctly in
  LibreOffice) exposed three bugs in the original DOCX loader, each confirmed by running
  the previous committed loader against the same files:
  (1) **Quarantine bypass:** text hidden through a Word *style* (rather than direct run
  formatting) was treated as visible, so a style-hidden prompt injection scored as a clean
  document and went straight into the scoring prompts. Styles (paragraph, character,
  `basedOn` chains) are now resolved.
  (2) **Fairness bug:** visibility was decided per paragraph from its first colour, so one
  white run in a paragraph made the loader drop the candidate's genuine visible text in that
  paragraph (in the fixture, their degree line). Visibility is now decided per run.
  (3) Every hidden-text DOCX raised a spurious PARSER_DIVERGENCE because the loader listed
  the same parse twice as two "parsers".
  Not modelled: theme colours, `docDefaults` sizing, highlight/shading, headers, footers,
  text boxes, comments and field codes; background is assumed white. The upload size limit
  is now `ingest.max_bytes` in config. DOCTYPE/ENTITY declarations in document.xml are
  rejected to avoid entity-expansion attacks.
- **A-14 (hidden-text quote).** Spec 8.2 requires the integrity report to quote at most 15
  words of hidden text, always labelled `hidden text reads: "..."`. This was missing until
  the end-to-end tests showed the Integrity tab never displayed it. The quote is built in
  code from the scanner's own evidence (never from model output, so a model cannot forge
  one) and only for HIDDEN_TEXT / INJECTION_HIDDEN findings. The Playwright suite asserts
  the injected phrase appears nowhere on the page outside those labelled quotes.
- **A-15 (DOCX hiding vectors, scanned PDFs, comments).** A second round of real-DOCX
  fixtures, each checked by rendering in LibreOffice, closed the vectors A-13 listed. Four
  were full quarantine bypasses under the previous loader, independently confirmed by running
  the committed loader on the same files (verdict clean, injection text in `visible_text`):
  a text box whose VML fallback differs from the rendered choice, a theme colour resolving to
  white, a highlight matching the text colour, and a white colour set in `docDefaults`. White
  or 1pt text in headers/footers, unrendered headers, unreferenced notes, and comments were
  also unseen; text boxes were counted up to four times; and a legitimate white heading on
  dark shading was a false positive. Contrast is now judged against the colour the text
  actually sits on (`Span.bg`). When Word and LibreOffice would render a theme colour
  differently, the less readable reading is judged.
  **Comments:** comment text never prints, so it is quarantined (never scored), but leftover
  reviewer comments are ordinary document hygiene, not a hiding trick. On their own they
  raise only an info-level `DOCUMENT_COMMENTS` note (like `OCR_LAYER`, never penalised);
  instructions aimed at the screener inside a comment are still `INJECTION_HIDDEN`. An
  earlier version of this change penalised every comment as HIDDEN_TEXT; that was caught in
  review before merge.
  **Scanned PDFs** with no text layer now produce a valid report (every requirement Not
  Enough Evidence, confidence 0, Review Manually, no integrity penalty) instead of an Error
  row. OCR via pytesseract is implemented but unavailable here, so it has only run through a
  monkeypatched test. Not modelled: alt text, field codes, character scaling/position, page
  background colour, table-style shading, off-page or obscured text boxes.
- **A-16 (LLM providers).** The Anthropic and Ollama paths had never executed. Exercising
  them through `httpx.MockTransport` (no live calls) found 11 bugs, among them: 4xx errors and
  a refused Ollama connection were retried with sleeps before falling back; a response that
  began with a non-text content block crashed; `usage` and `stop_reason` were never captured,
  so Module D's spec-required doubled-budget retry on truncation could never fire; and HTTP
  error text was not scrubbed. The API key is now redacted from every error and result.
  These paths are still untested against a real model: no API key or Ollama was available.
  When `ANTHROPIC_API_KEY` is set, Module D uses Anthropic even if the main provider is mock
  (Spec 6.2); report meta then sets `mock_mode: false` and records `interview_model`.
- **A-17 (names in uploads).** File uploads only know the filename, so the resume's own
  header name was never masked and survived into `candidate_profile`. `redact_for_scoring`
  now detects the conventional header (2-4 capitalised words followed by a contact line;
  the contact-line condition stops a title such as "Senior Backend Engineer" being taken for
  a name) when no name is supplied. Module endpoints added: `POST /v1/authenticity/assess`,
  `GET /v1/authenticity/{id}`, `POST /v1/skills/analyze`.
- **A-18 (GitHub request budget and a fairness bug).** Measured: the collector made 12 API
  requests per repo, up to 361 per candidate, with no cache, against GitHub's 60/hour
  unauthenticated limit. Worse, a rate limit hit partway through was read as "not found", so
  a rate-limited repo that did contain a skill made the claim UNSUPPORTED instead of
  UNVERIFIABLE: our own rate limit counted against the candidate. Now: one git-trees call per
  repo instead of nine, forks never opened, repos ranked by relevance to the claimed skills,
  early stop once required skills are covered, an 8-repo / 45-request cap, X-RateLimit-
  Remaining respected, and a SQLite response cache (TTL `cache.ttl_hours`; only 200/404
  cached; the token never stored). Measured after: 25 -> 5 requests for the fixture,
  361 -> 33 for a simulated 30-repo user, 0 on a repeat. A rate-limited collection is
  `partial`; claims it could have answered are UNVERIFIABLE, and the recruiter summary says
  plainly this is a data-collection limit, not evidence against the candidate.
- **A-19 (repo flags only for repos the resume relies on).** `fork_claimed_as_own` used to
  fire for any untouched fork, including one the candidate never mentioned (forking public
  repos is normal), and even fired on the original scenario-06 resume, which openly said
  "forked repository". Repo flags are now reported only for repos cited as evidence or named
  by a project claim. Flags are display-only and never changed a score or band. Scenario 06
  and the P5 perturbation were rewritten to test what the spec describes: a resume claiming
  a fork as original work. `fork_claimed_as_own` is now inferred from the listing
  (pushed_at <= created_at) because forks are no longer opened; this is weaker than the old
  commit check, and a fork with its own commits is never flagged.
- **A-20 (unified report schema).** `backend/app/schemas/report.py` models the whole Spec 13
  report (exported to `docs/report.schema.json`). Every report is validated before it is
  returned; a failure never crashes the pipeline but is recorded as `meta.schema_valid:
  false`. The schema exposed shape bugs, including two pipeline crashes (`UnboundLocalError`
  when Module A's interpreter or Module D failed) and, found in review, a safety bug: if the
  interpreter failed, the fallback used penalty 1.0 and "proceed", so an attack file scored
  at full value. Penalty, intent and action now always come from the scanner via
  `enforce_guardrails`. `verification_gaps.v1` is tagged on the authenticity block.
  mypy (lenient, Spec 4 "mypy-lite") is clean on all of `backend/app` and runs in CI.
- **A-21 (retention).** Candidate and authenticity reports older than
  `privacy.retain_raw_days` (30) are purged at startup and on demand (`Store.purge_expired`).
  Screening rows (no personal data) and the append-only audit log are kept; each purge is
  itself audited.
- **A-22 (synthetic-eval findings and fixes).** The synthetic corpus (`eval/synthetic/`,
  SYNTHETIC-ONLY) was committed first as an honest baseline, then defects it surfaced were
  fixed by spec semantics, not by tuning to it. The most serious: **authenticity bands were
  assigned from assessment_confidence instead of the authenticity score** (a bug in the
  original implementation, contrary to Spec 11 Stage 6), so a genuine candidate whose every
  checkable claim verified was banded NEEDS_VERIFICATION. False-accusation rate on the test
  split went 0.259 -> 0.000. Other fixes:
  - A LinkedIn date/title conflict now marks the ROLE claim CONTRADICTED with a citation.
    Resume-internal findings (overlap, graduation, anachronism) still force the band but
    do not mark a claim, because no second source disagrees.
  - Title synonyms (engineer/developer) are no longer contradictions; seniority inflation
    ("Lead" added) is. A lower rank is never flagged.
  - METRIC claims are compared with README figures conservatively. A conflicting figure needs
    strong context; loosening it reached P2 0.95 but falsely contradicted genuine candidates,
    so it was rejected. P2 stays 0.62.
  - GitHub: a manifest file name proves only its language; a framework needs the dependency
    in manifest content. Matching is by whole token or graph alias (Java no longer matches
    JavaScript; Vue.js, K8s, Apache Kafka resolve). An account with no public, or only
    forked, repos is "missing", not "checked and found nothing". `tutorial_clone` needs more
    than the word "tutorial".
  - **VERIFIED requires code evidence** (language bytes, manifest, dependency, CI workflow
    file). A README mention or a topic label is the candidate describing their own work and
    is WEAK. The corpus generator had made the same mistake and was corrected to declare
    real dependencies in manifests, as real repos do.
  - Projects match repos only on real name correspondence; an authored repo beats a fork.
    judge_confidence is a documented function of evidence strength. It was deliberately not
    pushed toward certainty to hit the ECE target (0.109 vs <= 0.10).
  - Resume parsing: "Languages: Python" labels stripped, CI/CD kept whole, Go/JS/TS kept,
    Achievements extracted, "Title, Company, dates" role lines parsed (re-enabling the
    LinkedIn employer cross-check). The generator's "Software Developering Intern" title bug
    was fixed.
  Still missed, reported as measured: P2 0.62 (target 0.80), P6 0.79 (0.95; private-work
  profiles WITH a LinkedIn export get a band from that corroboration rather than
  INSUFFICIENT_EVIDENCE, which is defensible but misses the spec's metric), AUROC 0.82
  (0.85), ECE 0.109 (0.10). No sample candidate's score, recommendation or band changed.
- **A-23 (first real-model run, Ollama `qwen2.5:7b-instruct` on an M4 / 16 GB).** Every LLM
  path had only ever met the mock or simulated HTTP. `eval/llm_smoke.py` runs real screenings
  and records which provider ACTUALLY served each call, so a silent fallback to mock cannot
  pass as success. Report: `eval/llm_smoke_report.md` (not in CI; needs a local model).
  Found against the real model, none visible to the mock:
  1. **Every report's explanation was blank.** The narrative prompt never named its keys, so
     the model's JSON had no `summary`/`strengths`/`risks`, and the report still validated
     because an empty string is a string. Keys are now stated; a wrong shape falls back to
     the deterministic fact-only template (noted in `meta.stages.narrative.fallback`); and the
     schema now rejects an empty summary.
  2. **Module D produced zero interview questions in every run.** Appendix B asks for "the
     output schema" but never states it; the model invented `{"questions": [...]}`. The Spec 12
     shape is now appended through a wrapper, keeping Appendix B verbatim.
  3. **A single generation ran for 22m17s.** Only Module D sent Ollama a `num_predict` cap, so
     a degenerate JSON-mode generation ran on long after the client's 60s timeout and blocked
     the single GPU queue for every later call. Every Ollama call is now capped.
  4. **The guilt-language guardrail used substring matching**, so ordinary words in real prose
     ("underlying", "familiar", "cheatsheet") replaced valid integrity explanations. Now whole
     words with inflections.
  After the fixes, across 2 runs x 4 cases: every call served by Ollama (no fallback), every
  report schema-valid, identical outcomes across runs, the attack PDF kept its 0.40 penalty
  and disqualify_review, no injected text in any output, no accusatory words, 8 interview
  questions per candidate that follow the evidence-level strategies. **Latency misses the
  spec:** about 50-100s per candidate (call p50 8-10s) against a 25s p50 target. Calls share one
  local GPU, so this is mostly hardware; it is reported, not tuned away.
- **A-24 (OpenRouter provider and a free-tier lesson).** Added an OpenAI-compatible
  `openrouter` provider (key `OPENROUTER_API_KEY` in the gitignored `.env`). Its first real run
  showed why `eval/llm_smoke.py` records who actually served each call: **27 of 30 calls were
  rate-limited (429) and silently fell back to mock, yet every report was schema-valid and
  safe**, so the run would otherwise have looked like a complete success. Diagnosis: the
  account was fine; Google's shared free capacity for the configured Gemma model was
  "temporarily rate-limited upstream", with no Retry-After. Fixes: the client now honours a
  server's Retry-After (capped at 20s); the default free model is Nemotron with free Gemma
  models listed as OpenRouter-side fallbacks; and the model that actually answered is recorded
  in `LLMResult.model`, so a routed fallback is never silent either. Module D's Claude model
  override is ignored for OpenRouter (only Anthropic can serve it). Re-run: 15/15 calls served
  by OpenRouter, call p50 3.1s, 10-47s per candidate (vs 50-100s on local Ollama), every report
  schema-valid, attack penalty kept, no injected text or accusatory language, 8 questions per
  candidate. Only one run was made to stay inside the free daily cap, so cross-run determinism
  for OpenRouter is not measured (it is for Ollama). LLM-written summaries are not yet checked
  against the structured facts they summarise.
