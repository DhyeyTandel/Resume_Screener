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
docker-build:  ## build the container image
	docker compose build
docker-up:     ## run the app container on :8077
	docker compose up
docker-up-ollama:  ## run the app plus the optional ollama container
	docker compose --profile ollama up
.PHONY: dev install test eval seed docker-build docker-up docker-up-ollama
