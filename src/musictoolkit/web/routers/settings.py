from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from musictoolkit.config import Config
from musictoolkit.ingest import organizer
from musictoolkit.integrations import lastfm_client, listenbrainz_client, musicbrainz_client
from musictoolkit.integrations.listenbrainz_client import ListenBrainzError
from musictoolkit.web.context import AppContext, get_ctx, same_path
from musictoolkit.web.scrobbler import describe_failure

router = APIRouter(prefix="/api")

# What the Settings page may change. Anything not listed here is not editable over HTTP.
EDITABLE: dict[str, set[str]] = {
    "library": {"canonical_scheme"},
    "musicbrainz": {"contact"},
    "listenbrainz": {"enabled", "username", "user_token", "scrobble", "now_playing"},
    "lastfm": {"enabled", "api_key", "api_secret"},
    "sync": {"device_scheme"},
    "app": {"rescan_on_launch", "lyrics_lrclib", "auto_update"},
}
TEMPLATE_FIELDS = {("library", "canonical_scheme"), ("sync", "device_scheme")}


def public_settings(cfg: Config) -> dict[str, Any]:
    """Settings as the UI sees them: secrets are reported as set/unset, never echoed back."""
    return {
        "library": {"roots": cfg.library.roots, "canonical_scheme": cfg.library.canonical_scheme},
        "musicbrainz": {"contact": cfg.musicbrainz.contact},
        "listenbrainz": {
            "enabled": cfg.listenbrainz.enabled,
            "username": cfg.listenbrainz.username,
            "has_token": bool(cfg.listenbrainz.user_token),
            "scrobble": cfg.listenbrainz.scrobble,
            "now_playing": cfg.listenbrainz.now_playing,
        },
        "lastfm": {
            "enabled": cfg.lastfm.enabled,
            "has_key": bool(cfg.lastfm.api_key),
            "has_secret": bool(cfg.lastfm.api_secret),
        },
        "sync": {"device_scheme": cfg.sync.device_scheme},
        "app": {"rescan_on_launch": cfg.app.rescan_on_launch, "lyrics_lrclib": cfg.app.lyrics_lrclib, "auto_update": cfg.app.auto_update},
    }


@router.get("/settings")
def get_settings(ctx: AppContext = Depends(get_ctx)) -> dict:
    return public_settings(ctx.config)


@router.put("/settings")
def put_settings(body: dict[str, dict[str, Any]], ctx: AppContext = Depends(get_ctx)) -> dict:
    """Partial update: only the keys sent change. Sending a secret as "" clears it."""
    section_objects = {
        "library": ctx.config.library,
        "musicbrainz": ctx.config.musicbrainz,
        "listenbrainz": ctx.config.listenbrainz,
        "lastfm": ctx.config.lastfm,
        "sync": ctx.config.sync,
        "app": ctx.config.app,
    }
    updates: list[tuple[Any, str, Any]] = []
    for section, values in body.items():
        if section not in EDITABLE:
            raise HTTPException(status_code=422, detail=f"unknown settings section {section!r}")
        for key, value in values.items():
            if key not in EDITABLE[section]:
                raise HTTPException(status_code=422, detail=f"{section}.{key} cannot be changed here")
            target = section_objects[section]
            current = getattr(target, key)
            if not isinstance(value, type(current)):
                raise HTTPException(status_code=422, detail=f"{section}.{key} must be a {type(current).__name__}")
            if (section, key) in TEMPLATE_FIELDS:
                try:
                    organizer.validate_scheme(value)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from None
            updates.append((target, key, value.strip() if isinstance(value, str) else value))

    for target, key, value in updates:  # validated first, so a bad field never half-applies the rest
        setattr(target, key, value)
    ctx.save_config()
    if "listenbrainz" in body and ctx.scrobbler:
        ctx.scrobbler.reset()  # a new token, or scrobbling switched on, should be tried right away
    return public_settings(ctx.config)


def _test_listenbrainz(cfg: Config) -> dict[str, Any]:
    lb = cfg.listenbrainz
    token = lb.user_token.strip()
    if not token:
        return {"ok": False, "message": "Paste your ListenBrainz user token first. You find it at listenbrainz.org/settings."}
    try:
        result = listenbrainz_client.validate_token(token)
    except ListenBrainzError as exc:
        return {"ok": False, "message": describe_failure(exc)}
    if not result["valid"]:
        return {"ok": False, "message": "ListenBrainz does not recognise this token. Copy it again from listenbrainz.org/settings."}
    name = result["user_name"] or ""
    note = ""
    if lb.username.strip() and name and lb.username.strip().lower() != name.lower():
        note = f" Your username setting says “{lb.username.strip()}”; change it to {name} so recommendations use this account."
    return {"ok": True, "message": f"Connected as {name}.{note}" if name else "ListenBrainz accepted the token."}


def _test_lastfm(cfg: Config) -> dict[str, Any]:
    key = cfg.lastfm.api_key.strip()
    if not key:
        return {"ok": False, "message": "Enter your Last.fm API key first."}
    ok, message = lastfm_client.check_key(key)
    return {"ok": ok, "message": message}


def _test_musicbrainz(cfg: Config) -> dict[str, Any]:
    contact = cfg.musicbrainz.contact.strip()
    if not contact:
        return {"ok": False, "message": "MusicBrainz asks every app to give a contact email. Add yours first."}
    ok, message = musicbrainz_client.ping(cfg.musicbrainz.app_name, cfg.musicbrainz.app_version, contact)
    return {"ok": ok, "message": message}


TESTS = {"listenbrainz": _test_listenbrainz, "lastfm": _test_lastfm, "musicbrainz": _test_musicbrainz}


@router.post("/settings/test/{service}")
def test_connection(service: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Try the saved key or token against its service, so a typo shows up here and not later as silence.
    Always answers 200 with {ok, message}; only the fixed service addresses are ever contacted."""
    tester = TESTS.get(service)
    if tester is None:
        raise HTTPException(status_code=404, detail="That service is not one Music Toolkit connects to")
    return tester(ctx.config)


class RootBody(BaseModel):
    path: str


@router.post("/library/roots", status_code=201)
def add_root(body: RootBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    from musictoolkit.web.routers.manage import submit_scan

    path = Path(body.path.strip().strip('"')).expanduser()
    if not path.is_dir():
        raise HTTPException(status_code=422, detail=f"Folder not found: {body.path}")
    resolved = str(path.resolve())
    if any(same_path(resolved, existing) for existing in ctx.config.library.roots):
        raise HTTPException(status_code=409, detail="That folder is already in your library")
    ctx.config.library.roots.append(resolved)
    ctx.save_config()
    job = submit_scan(ctx, [resolved], title=f"Scanning {path.name or resolved}")
    return {"roots": ctx.config.library.roots, "job": job.to_dict()}


@router.post("/library/roots/remove")
def remove_root(body: RootBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Stop watching a folder and hide its tracks. The files on disk are never touched."""
    remaining = [r for r in ctx.config.library.roots if not same_path(r, body.path)]
    if len(remaining) == len(ctx.config.library.roots):
        raise HTTPException(status_code=404, detail="That folder is not in your library")
    prefix = os.path.normpath(body.path).rstrip("\\/") + os.sep
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with ctx.db() as conn:
        hidden = conn.execute(
            "UPDATE tracks SET is_missing = 1 WHERE is_missing = 0 AND file_path LIKE ? ESCAPE '\\'",
            (escaped + "%",),
        ).rowcount
        conn.commit()
    ctx.config.library.roots = remaining
    ctx.save_config()
    return {"roots": remaining, "hidden_tracks": hidden}
