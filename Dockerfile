# Stage 1: build the React dashboard
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend-app/package.json frontend-app/package-lock.json ./
RUN npm ci
COPY frontend-app/index.html frontend-app/vite.config.ts frontend-app/tsconfig.json \
     frontend-app/tailwind.config.js frontend-app/postcss.config.js ./
COPY frontend-app/src/ src/
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCREENING_DB_PATH=/app/data/screening.db

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY --from=frontend /build/dist/ frontend-app/dist/
COPY eval/ eval/
COPY sample_data/ sample_data/
COPY sample_output/ sample_output/
COPY docs/ docs/

RUN mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

VOLUME ["/app/data"]
EXPOSE 8077

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8077/v1/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8077"]
