"""AI Resume Screening Assistant - decision support, never a hiring decision."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import err, router
from .config import cfg

app = FastAPI(
    title="AI Resume Screening Assistant",
    version="0.1.0",
    description="Recruiter-facing decision support. A human recruiter always decides.",
)


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Caps the request body before any parser sees it. A declared Content-Length over the limit
    is refused outright; otherwise (chunked uploads, or a header that lies) the bytes are counted
    as they stream in and the request is abandoned at the limit, so the body is never read into
    memory in full."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH") or not scope[
            "path"
        ].startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        limit = int(cfg("ingest.max_request_bytes", 50 * 1024 * 1024))
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._refuse(send, limit)
            return
        seen = 0
        started = False

        async def counted_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise _TooLarge
            return msg

        async def tracking_send(msg):
            nonlocal started
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        try:
            await self.app(scope, counted_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._refuse(send, limit)

    @staticmethod
    async def _refuse(send, limit: int) -> None:
        resp = JSONResponse(
            status_code=413,
            content=err(
                "REQUEST_TOO_LARGE",
                f"The upload is larger than the {limit / (1024 * 1024):g} MB request limit.",
                None,
                "Upload fewer or smaller files, or paste the text instead.",
            ),
            headers={"Connection": "close"},
        )
        await resp({"type": "http", "method": "POST"}, _no_receive, send)


async def _no_receive():
    return {"type": "http.disconnect"}


app.add_middleware(BodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.include_router(router)

_STD_ERROR_PATHS = ("/v1/authenticity", "/v1/skills")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Standard {code, message, field, remediation} body, only for the module endpoints;
    every other route keeps FastAPI's default 422 shape."""
    if not request.url.path.startswith(_STD_ERROR_PATHS):
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
    first = exc.errors()[0] if exc.errors() else {}
    loc = [str(p) for p in first.get("loc", []) if p != "body"]
    return JSONResponse(
        status_code=422,
        content={
            "code": "INVALID_REQUEST",
            "message": first.get("msg", "Invalid request body."),
            "field": ".".join(loc) or None,
            "remediation": "Check the request body against the API schema at /docs.",
        },
    )

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
