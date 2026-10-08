from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from musictoolkit.config import Config
from musictoolkit.ingest import organizer
from musictoolkit.web.context import AppContext, get_ctx

router = APIRouter(prefix="/api")

# What the Settings page may change. Anything not listed here is not editable over HTTP.
EDITABLE: dict[str, set[str]] = {
    "library": {"canonical_scheme"},
    "musicbrainz": {"contact"},
    "listenbrainz": {"enabled", "username", "user_token", "scrobble"},
    "lastfm": {"enabled", "api_key", "api_secret"},
    "sync": {"device_scheme"},
    "app": {"rescan_on_launch", "lyrics_lrclib"},
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
        },
        "lastfm": {
            "enabled": cfg.lastfm.enabled,
            "has_key": bool(cfg.lastfm.api_key),
            "has_secret": bool(cfg.lastfm.api_secret),
        },
        "sync": {"device_scheme": cfg.sync.device_scheme},
        "app": {"rescan_on_launch": cfg.app.rescan_on_launch, "lyrics_lrclib": cfg.app.lyrics_lrclib},
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
    return public_settings(ctx.config)


class RootBody(BaseModel):
    path: str


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


@router.post("/library/roots", status_code=201)
def add_root(body: RootBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    from musictoolkit.web.routers.manage import submit_scan

    path = Path(body.path.strip().strip('"')).expanduser()
    if not path.is_dir():
        raise HTTPException(status_code=422, detail=f"Folder not found: {body.path}")
    resolved = str(path.resolve())
    if any(_same(resolved, existing) for existing in ctx.config.library.roots):
        raise HTTPException(status_code=409, detail="That folder is already in your library")
    ctx.config.library.roots.append(resolved)
    ctx.save_config()
    job = submit_scan(ctx, [resolved], title=f"Scanning {path.name or resolved}")
    return {"roots": ctx.config.library.roots, "job": job.to_dict()}


@router.post("/library/roots/remove")
def remove_root(body: RootBody, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Stop watching a folder and hide its tracks. The files on disk are never touched."""
    remaining = [r for r in ctx.config.library.roots if not _same(r, body.path)]
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
