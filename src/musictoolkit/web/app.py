"""The web app: a JSON API plus the single-page UI. The desktop app binds it to 127.0.0.1; `mtk serve` can bind it further.

Access control (the API can move files and write tags, so a random web page
must not be able to drive it):
  * every request's Host header must be a loopback name, or one of the exact names
    a server was told it is reached by (blocks DNS rebinding);
  * /api/* needs the token, delivered once via /?token=... and kept in a
    SameSite=Strict cookie (so <audio>/<img> requests carry it too). The desktop
    app's token is new at every launch and its cookie ends with the window; a
    server's token is the same every time and its cookie lasts `login_days`.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from musictoolkit import __version__
from musictoolkit.config import Config
from musictoolkit.convert import store as convert_store
from musictoolkit.db.connection import connect
from musictoolkit.discs import store as disc_store
from musictoolkit.web.context import AppContext
from musictoolkit.web.routers import (
    convert,
    devices,
    discover,
    discs,
    duplicates,
    enrich,
    history,
    library,
    manage,
    missing,
    mixes,
    organize,
    playback,
    playlists,
    podcasts,
    radio,
    releases,
    scrobbling,
    settings,
    stats,
    system,
    tags,
)
from musictoolkit.net.proxy import Slots
from musictoolkit.radio.hub import RadioHub
from musictoolkit.web.hosts import LOOPBACK_HOSTS, clean_names, hostname_of
from musictoolkit.web.podcast_job import PodcastSchedule
from musictoolkit.web.radar_job import RadarSchedule
from musictoolkit.web.rescan_job import RescanSchedule
from musictoolkit.web.scrobbler import Scrobbler

logger = logging.getLogger("musictoolkit")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "mtk_session"

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
    allowed_hosts: Iterable[str] = (),
    login_days: int = 0,
    rescan_hours: float = 0,
) -> FastAPI:
    """`allowed_hosts`: the names besides the loopback ones that this app answers to (a server's). `login_days`: how long the cookie
    that the token buys lasts (0: until the browser window is closed). `rescan_hours`: look for new songs by itself this often (0: never)."""
    answers_to = LOOPBACK_HOSTS | set(clean_names(allowed_hosts))
    ctx = AppContext(
        db_path=Path(db_path).absolute(), config_path=Path(config_path).absolute(), config=config, token=token
    )
    connect(ctx.db_path).close()  # first launch: create the folder and apply migrations now
    try:  # a rip that was running when the app stopped did not finish: say so, and remove the half-written files it left
        with ctx.db() as conn:
            disc_store.recover(conn, ctx.data_dir / "cache" / "discs")
            disc_store.recover_burns(conn, ctx.data_dir / "cache" / "discs" / "burn")
            convert_store.recover(conn, ctx.data_dir / "cache" / "convert")
    except Exception:
        logger.warning("Could not tidy up after an interrupted rip, burn or conversion", exc_info=True)
    ctx.scrobbler = Scrobbler(ctx)
    ctx.radar = RadarSchedule(ctx)
    ctx.radio = RadioHub()
    ctx.podcast_slots = Slots(6)
    ctx.podcasts = PodcastSchedule(ctx)
    ctx.rescan = RescanSchedule(ctx, rescan_hours)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if rescan_on_start and ctx.config.app.rescan_on_launch and ctx.config.library.roots:
            manage.submit_scan(ctx, list(ctx.config.library.roots), title="Refreshing library")
        ctx.scrobbler.start()
        ctx.radar.start()
        ctx.podcasts.start()
        ctx.rescan.start()
        try:
            yield
        finally:
            ctx.rescan.stop()
            ctx.podcasts.stop()
            ctx.radar.stop()
            ctx.scrobbler.stop()

    app = FastAPI(title="Music Toolkit", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if hostname_of(request.headers.get("host", "")) not in answers_to:
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

    for module in (system, library, playback, playlists, settings, manage, discover, history, devices, tags, enrich, organize, duplicates, missing, scrobbling, stats, mixes, releases, radio, podcasts, discs, convert):
        app.include_router(module.router)

    @app.get("/", include_in_schema=False)
    def index(request: Request, token: str | None = None):
        response = FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
        if ctx.token:
            if token and hmac.compare_digest(token, ctx.token):
                response.set_cookie(
                    COOKIE_NAME, ctx.token, httponly=True, samesite="strict", path="/",
                    secure=request.url.scheme == "https",  # (a browser keeps a Secure cookie only from an https page: uvicorn reports https when a proxy on this machine says so)
                    max_age=login_days * 86400 if login_days > 0 else None,
                )
            elif not hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""), ctx.token):
                return HTMLResponse(LOCKED_PAGE, status_code=401)
        return response

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def healthz() -> dict:
        """For a monitor: the app is up, and whether the library folders are there. No token (a monitor has none), no paths."""
        there = [Path(root).is_dir() for root in ctx.config.library.roots]
        return {
            "status": "ok" if all(there) else "degraded",
            "version": __version__,
            "library": {"folders": len(there), "available": sum(there)},
        }

    app.mount("/static", _Static(directory=STATIC_DIR), name="static")
    return app
