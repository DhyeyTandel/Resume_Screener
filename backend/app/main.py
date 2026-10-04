"""AI Resume Screening Assistant - decision support, never a hiring decision."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router

app = FastAPI(
    title="AI Resume Screening Assistant",
    version="0.1.0",
    description="Recruiter-facing decision support. A human recruiter always decides.",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.include_router(router)

_ROOT = Path(__file__).resolve().parents[2]
_FALLBACK = _ROOT / "frontend" / "index.html"
_DIST = _ROOT / "frontend-app" / "dist"

if (_DIST / "index.html").is_file():
    # Built React app: static assets first, then index.html at "/".
    if (_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(_DIST / "index.html")

    @app.get("/{filename}", include_in_schema=False)
    async def root_file(filename: str) -> FileResponse:
        # favicon and other top-level files copied from public/
        target = _DIST / filename
        if "/" not in filename and target.is_file():
            return FileResponse(target)
        raise HTTPException(404, "Not found")
else:

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(_FALLBACK)
