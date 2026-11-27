"""The web app: a JSON API plus the single-page UI. The desktop app binds it to 127.0.0.1; `mtk serve` can bind it further.

Access control (the API can move files and write tags, so a random web page
must not be able to drive it):
  * every request's Host header must be a loopback name, or one of the exact names
    a server was told it is reached by (blocks DNS rebinding);
  * /api/* needs the token, delivered once via /?token=... (or typed into the
    sign-in page, which asks for it when a browser has no cookie) and kept in a
    SameSite=Lax cookie (so <audio>/<img> requests carry it too, and so does the
    request that opens the app from an icon or from a link in another app: Strict
    leaves the cookie out of those, and a device would be asked for the token it
    already gave). Lax lets a cookie ride along on a link that is followed from
    another site, so /api/* refuses a link followed from another site, and any
    request that changes something, that the browser says another site (or another
    name under the same domain) made. The desktop app's token is new at every launch and its cookie
    ends with the window; a server's token is the same every time and its cookie
    lasts `login_days`, and the app keeps a key besides (remember.py) that gets
    the cookie back when the browser has lost it;
  * a token that someone typed, followed in a link or sent from a program is
    counted when it is wrong, and after a few wrong ones none is looked at for
    a minute. A device that is already signed in is not affected;
  * a server that is told which Tailscale accounts to trust (`tailscale_logins`, behind
    `tailscale serve`) signs those in without the token, and refuses what came
    through Tailscale Funnel: see tailnet.py;
  * /rest, the Subsonic API for the phone and Mac apps made for that protocol, is not /api: it takes the credentials that protocol has (see
    subsonic/auth.py), or a login Tailscale vouches for;
  * the app's own files (/static, and /sw.js, which an installed app needs from
    the root) hold nothing private and are sent to anyone who asks, the way a
    browser asks for them before it has a cookie.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from musictoolkit import __version__
from musictoolkit.config import Config
from musictoolkit.convert import store as convert_store
from musictoolkit.db.connection import connect
from musictoolkit.discs import store as disc_store
from musictoolkit.ingest.scanner import unavailable_reason
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
from musictoolkit.web import remember, tailnet
from musictoolkit.web.backup_job import BackupSchedule
from musictoolkit.web.hosts import LOOPBACK_HOSTS, clean_names, hostname_of
from musictoolkit.web.podcast_job import PodcastSchedule
from musictoolkit.web.radar_job import RadarSchedule
from musictoolkit.web.rescan_job import RescanSchedule
from musictoolkit.web.scrobbler import Scrobbler
from musictoolkit.web.signin import GuessLimiter, arrival, same_token, sign_in_page
from musictoolkit.web.subsonic import router as subsonic_router

logger = logging.getLogger("musictoolkit")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "mtk_session"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https: http:; "
    "style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)

ROUTERS = (
    system, library, playback, playlists, settings, manage, discover, history, devices, tags, enrich, organize, duplicates, missing,
    scrobbling, stats, mixes, releases, radio, podcasts, discs, convert,
)

# GET routes that answer a HEAD request as well, so a monitor or a player can ask for headers only. Not these two: each opens a
# connection to another server to answer, and a request for headers only must not do that.
NO_HEAD = frozenset({"/api/radio/stations/{station_id}/stream", "/api/podcasts/episodes/{episode_id}/stream"})


def answer_head(routes) -> None:
    """Make each route that reads answer HEAD too (FastAPI answers a HEAD request only on a route that was made for it)."""
    for route in routes:
        if isinstance(route, APIRoute) and "GET" in route.methods and route.path not in NO_HEAD:
            route.methods.add("HEAD")


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
    tailscale_logins: Iterable[str] = (),
) -> FastAPI:
    """`allowed_hosts`: the names besides the loopback ones that this app answers to (a server's). `login_days`: how long the cookie
    that the token buys lasts (0: until the browser window is closed). `rescan_hours`: look for new songs by itself this often (0: never).
    `tailscale_logins`: the Tailscale accounts that are signed in without the token, on Tailscale's word (a server that listens on
    this computer only, behind `tailscale serve`: `mtk serve` checks that before it passes the list on)."""
    answers_to = LOOPBACK_HOSTS | set(clean_names(allowed_hosts))
    logins = frozenset(tailnet.clean_logins(tailscale_logins))
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
    ctx.backups = BackupSchedule(ctx)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if rescan_on_start and ctx.config.app.rescan_on_launch and ctx.config.library.roots:
            manage.submit_scan(ctx, list(ctx.config.library.roots), title="Refreshing library")
        ctx.scrobbler.start()
        ctx.radar.start()
        ctx.podcasts.start()
        ctx.rescan.start()
        ctx.backups.start()
        try:
            yield
        finally:
            ctx.backups.stop()
            ctx.rescan.stop()
            ctx.podcasts.stop()
            ctx.radar.stop()
            ctx.scrobbler.stop()

    app = FastAPI(title="Music Toolkit", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    limiter = app.state.limiter = GuessLimiter()

    def signed_in_by(request: Request) -> str | None:
        """How the cookie signed this device in: "token", "tailscale" (a login that is still on the list), or None (no cookie, or one that
        is no good any more: another token's, or a login that was taken off the list). Only asked where there is a token."""
        cookie = request.cookies.get(COOKIE_NAME, "")
        if same_token(cookie, ctx.token):
            return "token"
        if tailnet.cookie_login(cookie, ctx.token, logins):
            return "tailscale"
        return None

    def signed_in(request: Request) -> bool:
        return signed_in_by(request) is not None

    def seen(request: Request) -> tailnet.Seen:
        """What Tailscale says about the device that made the request (nothing, unless this server runs behind `tailscale serve`)."""
        return tailnet.look(request.headers, logins)

    def vouched_login(request: Request) -> str | None:
        """The login Tailscale vouches for in this request, if this server signs that account in (for the calls that do not carry the cookie)."""
        said = seen(request)
        return said.login if said.vouched else None

    app.state.vouched = vouched_login

    def try_token(candidate: str) -> tuple[bool, int]:
        """(is it the token, seconds before the next try). A wrong one is counted; while too many have been wrong none is looked at."""
        if pause := limiter.wait():
            return False, pause
        if same_token(candidate, ctx.token):
            return True, 0
        limiter.wrong()
        return False, 0

    def give_cookie(response: Response, request: Request, value: str | None = None) -> Response:
        response.set_cookie(
            COOKIE_NAME, value or ctx.token, httponly=True, samesite="lax", path="/",
            secure=request.url.scheme == "https",  # (a browser keeps a Secure cookie only from an https page: uvicorn reports https when a proxy on this machine says so)
            max_age=login_days * 86400 if login_days > 0 else None,
        )
        return response

    def sign_in_as(response: Response, request: Request, login: str) -> Response:
        """The cookie of a device that Tailscale vouched for (made from the token and the login: see tailnet.session_cookie)."""
        return give_cookie(response, request, tailnet.session_cookie(ctx.token, login))

    def asking(request: Request, status: int, problem: str = "", wait: int = 0) -> HTMLResponse:
        """The page with the box for the token (401), or the same page saying to wait (429). The first visit's page says what the request carried, and the log
        says it once (a device that is asked again, though it was told to stay signed in, is the thing to find out about)."""
        headers = {"Cache-Control": "no-store", **({"Retry-After": str(wait)} if wait else {})}
        said = seen(request)
        arrived = arrival(request.headers, request.cookies.get(COOKIE_NAME)) if status == 401 and not problem else None
        if arrived is not None and request.method == "GET":
            logger.info("Sign-in page shown: %s", arrived.line())
        page = sign_in_page(
            problem=problem, login_days=login_days, tailscale_login=said.login, tailscale_on=bool(logins), tailscale_allowed=said.allowed, arrived=arrived
        )
        return HTMLResponse(page, status_code=status, headers=headers)

    def from_another_site(request: Request) -> bool:
        """Does the browser say that this request is a link followed from, or a change asked for by, a page of another site (or of a sibling name of this one, under the
        same domain: `Sec-Fetch-Site` says "same-site" then)? A song, a cover or a call that only reads, loaded by such a page, is not asked about: the page cannot read
        the answer, nothing is changed, and a browser that was wrong about where a song's request came from must not stop the music. What is not a browser's (a program,
        the desktop shell) does not say, and is not asked."""
        elsewhere = request.headers.get("sec-fetch-site") in ("cross-site", "same-site")
        return elsewhere and (request.method not in ("GET", "HEAD") or request.headers.get("sec-fetch-mode") == "navigate")

    def too_many(wait: int) -> str:
        return f"Too many wrong tokens. Try again in {wait} seconds."

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if tailnet.through_funnel(request.headers):  # (Tailscale marks a request that came from the public internet: this server is for a private network)
            return JSONResponse({"detail": "this server is not available through Tailscale Funnel"}, status_code=403)
        if hostname_of(request.headers.get("host", "")) not in answers_to:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        vouched_for: str | None = None  # the login that Tailscale vouched for, when that is what lets this request in: it gets the cookie
        if ctx.token and request.url.path.startswith("/api/") and signed_in(request) and from_another_site(request):
            # (the cookie is Lax, which lets it ride along on a link that is followed from another site; and a page on a sibling name under the same domain, which
            # Strict and Lax both count as the same site, could ask for a change. Neither is let in, and nothing is taken away from the device: it is not told to forget the cookie)
            return JSONResponse({"detail": "this request came from a page of another site"}, status_code=403)
        if ctx.token and request.url.path.startswith("/api/") and not signed_in(request):
            said = seen(request)
            if said.vouched:
                vouched_for = said.login
            else:
                offered = request.headers.get("x-mtk-token")  # (a program: the desktop shell, a script)
                right, pause = try_token(offered) if offered else (False, 0)
                if not right:
                    refused = JSONResponse(
                        {"detail": f"too many wrong tokens: try again in {pause} seconds" if pause else "unauthorized"},
                        status_code=429 if pause else 401,
                        headers={"Retry-After": str(pause)} if pause else None,
                    )
                    if request.cookies.get(COOKIE_NAME):  # the cookie of a token that was changed since: have the browser drop it
                        refused.delete_cookie(COOKIE_NAME, path="/")
                    return refused
        response = await call_next(request)
        if vouched_for is not None:
            sign_in_as(response, request, vouched_for)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error serving %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    for module in ROUTERS:
        answer_head(module.router.routes)  # (before the router is included: how FastAPI keeps an included router's routes differs between its versions)
        app.include_router(module.router)
    app.include_router(subsonic_router)  # (/rest: the Subsonic API, which says who may ask in its own way)

    @app.get("/", include_in_schema=False)
    def index(request: Request, token: str | None = None):
        page = FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
        if not ctx.token:
            return page
        if signed_in(request):
            if token and same_token(token, ctx.token):  # (the link was opened again: the cookie is renewed)
                return give_cookie(page, request)
            return page
        if (said := seen(request)).vouched:  # (Tailscale says who this is, and the server signs that account in: no token is asked for or counted)
            return sign_in_as(page, request, said.login)
        if not token:
            return asking(request, 401)
        right, pause = try_token(token)
        if pause:
            return asking(request, 429, too_many(pause), pause)
        if not right:
            return asking(request, 401, "That is not the token.")
        return give_cookie(page, request)

    @app.get("/signin", include_in_schema=False)
    def signin_address() -> Response:
        """The address of the form, opened by hand or reloaded after a try: the page that asks for the token is the one at /."""
        return RedirectResponse("/", status_code=303)

    @app.post("/signin", include_in_schema=False)
    def signin(request: Request, token: str = Form("")):
        """The sign-in page's form: the right token gets the cookie and the app, a wrong one the page again."""
        if not ctx.token or signed_in(request):
            return RedirectResponse("/", status_code=303)
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):  # (a form on another site guessing tokens through this browser)
            return JSONResponse({"detail": "a sign-in has to be made from this app's own page"}, status_code=403)
        if (said := seen(request)).vouched:
            return sign_in_as(RedirectResponse("/", status_code=303), request, said.login)
        right, pause = try_token(token)
        if pause:
            return asking(request, 429, too_many(pause), pause)
        if not right:
            return asking(request, 401, "That is not the token.")
        return give_cookie(RedirectResponse("/", status_code=303), request)

    @app.post("/signin/restore", include_in_schema=False)
    def restore(request: Request, key: str = Form("")) -> Response:
        """The sign-in page's script, for a device whose browser lost the cookie but kept the key (see remember.py): the right key gets the cookie that the token
        buys (204), a wrong one nothing (401, and it is counted like a wrong token). 404 where devices are not kept signed in."""
        if not ctx.token or login_days <= 0:
            return JSONResponse({"detail": "this server does not keep devices signed in"}, status_code=404)
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return JSONResponse({"detail": "a sign-in has to be made from this app's own page"}, status_code=403)
        if signed_in(request):
            return Response(status_code=204)
        if pause := limiter.wait():
            return JSONResponse({"detail": too_many(pause)}, status_code=429, headers={"Retry-After": str(pause)})
        if not remember.valid(key, ctx.token, time.time()):
            limiter.wrong()
            return JSONResponse({"detail": "that key is not good"}, status_code=401)
        return give_cookie(Response(status_code=204), request)

    @app.get("/api/session", include_in_schema=False)
    def session(request: Request) -> dict:
        """How this device is signed in, and what Tailscale says about it (for Settings): `by` is "token", "tailscale" or "open" (the app has
        no token). `tailscale.login` is the login Tailscale put in this very request: the person's own, shown to the person. `remember_days` is how long
        a device that signed in with the token is kept signed in (0: not at all, as in the desktop app), and so whether it should keep a key."""
        said = seen(request)
        if not ctx.token:
            by = "open"
        else:
            by = signed_in_by(request) or ("tailscale" if said.vouched else None)
        return {
            "by": by, "remember_days": login_days if ctx.token else 0,
            "tailscale": {"on": bool(logins), "login": said.login, "allowed": said.allowed, "own_page": said.own_page},
        }

    @app.post("/api/session/remember", include_in_schema=False)
    def remember_key(request: Request) -> JSONResponse:
        """A key for this device to keep (see remember.py), for a device that is signed in with the token (one that Tailscale signs in has no use for it) where
        devices are kept signed in. It is made again each time, so the one a device keeps never runs out while the device is in use."""
        if not ctx.token or login_days <= 0:
            return JSONResponse({"detail": "this server does not keep devices signed in"}, status_code=404)
        if signed_in_by(request) != "token":
            return JSONResponse({"detail": "only a device that is signed in with the token keeps a key"}, status_code=403)
        expires = int(time.time()) + login_days * 86400
        return JSONResponse({"key": remember.make(ctx.token, expires), "expires": expires, "days": login_days}, headers={"Cache-Control": "no-store"})

    @app.post("/api/session/signout", include_in_schema=False)
    def sign_out() -> Response:
        """Take this device's cookie away (its page forgets the key, and the device is asked for the token the next time)."""
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> Response:
        """The service worker of the installed app. It has to be sent from the root of the address, not from /static/, to be in charge of
        the whole app; it holds nothing private, so no token is asked for (a browser fetches it without the cookie when it checks for a
        new one). Its version is this app's, so a new version of the app is a new file."""
        source = (STATIC_DIR / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", __version__)
        return Response(source, media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def healthz() -> dict:
        """For a monitor: the app is up, whether the library folders are there, and whether the copies of the database are being made. No token (a monitor has none), no paths."""
        with ctx.db() as conn:
            there = [unavailable_reason(conn, Path(root)) is None for root in ctx.config.library.roots]
        overdue = ctx.backups.overdue()
        return {
            "status": "ok" if all(there) and not overdue else "degraded",
            "version": __version__,
            "library": {"folders": len(there), "available": sum(there)},
            "backups": "overdue" if overdue else "ok" if ctx.backups.settings().hours > 0 else "off",
        }

    answer_head(app.routes)  # (the page, the sign-in address and the health check are the app's own routes)

    app.mount("/static", _Static(directory=STATIC_DIR), name="static")
    return app
