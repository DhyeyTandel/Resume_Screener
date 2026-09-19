"""AI Resume Screening Assistant - decision support, never a hiring decision."""
from __future__ import annotations
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

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

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(_FRONTEND)
