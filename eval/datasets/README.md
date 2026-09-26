# Datasets

Empty by design. The Section 16.1 targets that need a real dataset
(claim-extraction F1, claim-status macro-F1, AUROC, calibration ECE, the
fairness false-flag gap) require consenting candidates' real resumes plus
their GitHub/LinkedIn/portfolio data, hand-labeled by two annotators. None of
that has been collected. `eval/run_eval.py` measures everything that can be
measured honestly without it - see `eval/report.md`'s "Not evaluated here"
section for the exact list and why each is missing rather than estimated.
