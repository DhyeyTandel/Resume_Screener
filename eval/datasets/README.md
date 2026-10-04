# Datasets

`synthetic/` holds a deterministic, generated Module B corpus (300 candidates, seed in
`manifest.json`, fixed stratified 90 train / 210 test split) with ground truth known by
construction. It is built by `eval/synthetic/generate.py` and measured by
`eval/synthetic/metrics.py` on the test split only. Results are SYNTHETIC-ONLY: the corpus
and the metrics were written by the same hand, so they find bugs and track regressions but
do not establish real-world performance.

What still needs real data: consenting candidates' actual resumes with their GitHub,
LinkedIn and portfolio data, hand-labelled by two annotators (Spec 16.1). None has been
collected.
