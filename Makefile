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
frontend-install:  ## install frontend dependencies (needs Node 22+)
	cd frontend-app && npm ci
frontend-dev:      ## run the Vite dev server on :5173 (proxies /v1 to :8077)
	cd frontend-app && npm run dev
frontend-build:    ## build the React dashboard into frontend-app/dist
	cd frontend-app && npm run build
docker-build:  ## build the container image
	docker compose build
docker-up:     ## run the app container on :8077
	docker compose up
docker-up-ollama:  ## run the app plus the optional ollama container
	docker compose --profile ollama up
.PHONY: dev install test eval seed frontend-install frontend-dev frontend-build docker-build docker-up docker-up-ollama
