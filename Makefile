VENV := .venv/bin
dev:      ## run backend + dashboard on :8077
	$(VENV)/uvicorn backend.app.main:app --port 8077 --reload
install:  ## create the venv and install dependencies
	python3 -m venv .venv && $(VENV)/pip install -r requirements.txt
test:     ## run the full test suite
	cd backend && ../$(VENV)/python -m pytest -q
eval:     ## run the evaluation harness
	$(VENV)/python eval/run_eval.py
seed:     ## screen every sample resume and write sample_output/
	$(VENV)/python eval/seed.py
.PHONY: dev install test eval seed
