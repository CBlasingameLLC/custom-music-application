"""The /rest address: one route that finds the call by its name, checks who is asking and that the client speaks a version of the API this server does, and
answers in the form the client asked for (`f`: xml, json or jsonp). Every call answers with HTTP 200: a failure is `status="failed"` and a number in the answer,
which is how a Subsonic client learns of it (a song or a cover is the exception: the file itself, or, when it cannot be had, the same kind of answer)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from starlette.responses import Response

from musictoolkit.web.context import AppContext
from musictoolkit.web.subsonic import render
from musictoolkit.web.subsonic.auth import authenticate, check_version
from musictoolkit.web.subsonic.errors import GENERIC, MISSING, SubsonicError, from_http
from musictoolkit.web.subsonic.params import Params

logger = logging.getLogger("musictoolkit")
router = APIRouter()


@dataclass
class Call:
    """What a call has to work with: the app, the request, what was sent, and the name the client gave (what it asked as)."""

    ctx: AppContext
    request: Request
    params: Params
    user: str


Handler = Callable[[Call], "dict[str, Any] | Response"]
REGISTRY: dict[str, Handler] = {}
PUBLIC: set[str] = set()  # the calls that need no credentials (OpenSubsonic says that of getOpenSubsonicExtensions: a client asks it before it signs in)


def endpoint(*names: str, public: bool = False) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        for name in names:
            REGISTRY[name] = handler
            if public:
                PUBLIC.add(name)
        return handler

    return register


def _ask(name: str, request: Request, params: Params) -> dict[str, Any] | Response:
    handler = REGISTRY.get(name)
    if handler is None:
        raise SubsonicError(GENERIC, f"This server does not do {name}")
    ctx: AppContext = request.app.state.ctx
    user = params.get("u") or ""
    if name not in PUBLIC:
        check_version(params)
        if ctx.token:
            vouched = request.app.state.vouched(request)  # (a login Tailscale vouches for: no password is asked)
            if vouched is None:
                user = authenticate(params, ctx.token, request.app.state.limiter)
            else:
                user = user or vouched.split("@")[0]
    return handler(Call(ctx=ctx, request=request, params=params, user=user or "mtk"))


async def _params(request: Request) -> Params:
    sources = [request.query_params.multi_items()]
    if request.method == "POST":
        try:
            form = await request.form()  # (OpenSubsonic's formPost: the same parameters in the body)
            sources.append(form.multi_items())  # type: ignore[arg-type]
        except Exception:
            pass
    return Params(*sources)


def _answer(reply: dict[str, Any], params: Params) -> Response:
    form = (params.get("f") or "xml").lower()
    headers = {"Cache-Control": "no-store"}
    if form == "json":
        return Response(render.to_json(reply), media_type="application/json", headers=headers)
    if form == "jsonp":
        callback = params.get("callback")
        if callback is None:
            return Response(render.to_json(render.failure(MISSING, "Required parameter is missing: callback")), media_type="application/json", headers=headers)
        try:
            return Response(render.to_json(reply, callback), media_type="text/javascript", headers=headers)
        except ValueError:
            return Response(render.to_json(render.failure(GENERIC, "callback is not a function name")), media_type="application/json", headers=headers)
    return Response(render.to_xml(reply), media_type="text/xml", headers=headers)


@router.api_route("/rest/{name}", methods=["GET", "POST", "HEAD"], include_in_schema=False)
async def rest(name: str, request: Request) -> Response:
    params = await _params(request)
    name = name.removesuffix(".view")  # (the older clients write every call that way)
    try:
        reply = await run_in_threadpool(_ask, name, request, params)
        if isinstance(reply, Response):
            return reply
        reply = render.envelope(reply)
    except SubsonicError as error:
        reply = render.failure(error.code, error.message)
    except HTTPException as error:  # (what the rest of the app says with a status, in this protocol's words)
        said = from_http(error)
        reply = render.failure(said.code, said.message)
    except Exception:
        logger.exception("Unhandled error answering /rest/%s", name)  # (the name of the call only: the credentials of a client are in its query)
        reply = render.failure(GENERIC, "The server could not answer that")
    return _answer(reply, params)
