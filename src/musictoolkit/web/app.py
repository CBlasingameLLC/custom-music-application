"""The local web app: a JSON API plus the single-page UI, bound to 127.0.0.1.

Access control (the API can move files and write tags, so a random web page
must not be able to drive it):
  * every request's Host header must be a loopback name (blocks DNS rebinding);
  * /api/* needs the per-launch token, delivered once via /?token=... and kept
    in a SameSite=Strict cookie (so <audio>/<img> requests carry it too).
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from musictoolkit import __version__
from musictoolkit.config import Config
from musictoolkit.db.connection import connect
from musictoolkit.web.context import AppContext
from musictoolkit.web.routers import (
    devices,
    discover,
    history,
    library,
    manage,
    playback,
    playlists,
    settings,
    system,
    tags,
)

logger = logging.getLogger("musictoolkit")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "mtk_session"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https: http:; "
    "style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)

LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><title>Music Toolkit</title>
<body style="font-family:system-ui;max-width:34rem;margin:4rem auto;padding:0 1rem">
<h1>Open Music Toolkit from the app</h1>
<p>This page is protected. Launch the Music Toolkit desktop app, or use the full address
(including <code>?token=...</code>) that <code>mtk dashboard</code> prints.</p></body>"""


def _hostname(header: str) -> str:
    """Host header without its port: '127.0.0.1:4533' -> '127.0.0.1', '[::1]:4533' -> '[::1]'."""
    header = header.strip().lower()
    if header.startswith("["):
        return header.split("]")[0] + "]"
    return header.rsplit(":", 1)[0] if ":" in header else header


class _Static(StaticFiles):
    """Static files that always revalidate, so an updated app never serves stale scripts."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    *,
    db_path: Path,
    config_path: Path,
    config: Config,
    token: str | None = None,
    rescan_on_start: bool = False,
) -> FastAPI:
    ctx = AppContext(
        db_path=Path(db_path).absolute(), config_path=Path(config_path).absolute(), config=config, token=token
    )
    connect(ctx.db_path).close()  # first launch: create the folder and apply migrations now

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if rescan_on_start and ctx.config.app.rescan_on_launch and ctx.config.library.roots:
            manage.submit_scan(ctx, list(ctx.config.library.roots), title="Refreshing library")
        yield

    app = FastAPI(title="Music Toolkit", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if _hostname(request.headers.get("host", "")) not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if ctx.token and request.url.path.startswith("/api/"):
            supplied = request.cookies.get(COOKIE_NAME) or request.headers.get("x-mtk-token") or ""
            if not hmac.compare_digest(supplied, ctx.token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error serving %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    for module in (system, library, playback, playlists, settings, manage, discover, history, devices, tags):
        app.include_router(module.router)

    @app.get("/", include_in_schema=False)
    def index(request: Request, token: str | None = None):
        response = FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
        if ctx.token:
            if token and hmac.compare_digest(token, ctx.token):
                response.set_cookie(COOKIE_NAME, ctx.token, httponly=True, samesite="strict", path="/")
            elif not hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""), ctx.token):
                return HTMLResponse(LOCKED_PAGE, status_code=401)
        return response

    app.mount("/static", _Static(directory=STATIC_DIR), name="static")
    return app
