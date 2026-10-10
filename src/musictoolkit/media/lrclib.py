"""Lyrics from lrclib.net, a free community-run lyrics service, for songs whose files have none.

Nothing here runs unless the person turned lookups on in Settings or pressed Look up on a song. What goes out is the
artist, title, album and length of that one song. What comes back is kept in the app's own database (table
`lyrics_cache`), never written next to the music.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any

import requests

from musictoolkit import __version__
from musictoolkit.media.lyrics import parse_lrc

BASE_URL = os.environ.get("MTK_LRCLIB_URL", "https://lrclib.net/api").rstrip("/")  # the variable is for tests and the packaged-app check
TIMEOUT = 10.0
MAX_BYTES = 2_000_000  # an answer is a few kilobytes of text; anything near this is not one
DEADLINE = 30.0  # seconds for the whole of one answer, however slowly it trickles in
USER_AGENT = f"Music Toolkit/{__version__} (https://github.com/CBlasingameLLC/custom-music-application)"
DURATION_SLACK = 3.0  # seconds a version of the song may differ from the file and still keep its timing
NONE_KEPT_FOR = 14 * 86400  # LRCLIB grows: a song it did not have is asked about again after two weeks
RETRY_AFTER_TROUBLE = 300.0  # seconds before a song whose lookup failed is tried again on its own


class LrclibError(Exception):
    """LRCLIB could not be reached or was in trouble. Nothing is remembered about the song; asking again later may work."""


def _words(text: str | None) -> str:
    return re.sub(r"[\W_]+", "", (text or "").casefold())


def query_key(artist: str | None, title: str | None, album: str | None, duration: float | None) -> str:
    """What was asked about a song. When a tag or the length changes the key changes and the song is asked about again."""
    text = "\x1f".join([artist or "", title or "", album or "", str(round(duration)) if duration else ""])
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _get(path: str, params: dict[str, Any]) -> Any | None:
    """One request. None means LRCLIB answered that it has no such thing; trouble of any kind raises LrclibError."""
    started = time.monotonic()
    try:
        # No redirects (the address is fixed), and the answer is read in pieces so a huge or endless one is cut off.
        with requests.get(f"{BASE_URL}{path}", params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True, allow_redirects=False) as response:
            if response.status_code == 404:
                return None
            if response.status_code in (429, 503) or response.status_code >= 500:
                raise LrclibError("LRCLIB is busy right now. Try again in a little while.")
            if response.status_code != 200:
                raise LrclibError(f"LRCLIB answered with error {response.status_code}.")
            pieces, size = [], 0
            for piece in response.iter_content(chunk_size=65536):
                size += len(piece)
                if size > MAX_BYTES or time.monotonic() - started > DEADLINE:
                    raise LrclibError("LRCLIB sent an answer that was too large or too slow to use.")
                pieces.append(piece)
    except requests.RequestException as exc:
        raise LrclibError(f"Could not reach LRCLIB ({type(exc).__name__}).") from exc
    try:
        return json.loads(b"".join(pieces))
    except ValueError as exc:  # includes bytes that are not UTF-8
        raise LrclibError("LRCLIB sent an answer that could not be read.") from exc


def _shape(hit: dict[str, Any]) -> dict[str, Any] | None:
    synced = parse_lrc(hit.get("syncedLyrics") or "") or None
    plain = (hit.get("plainLyrics") or "").strip() or None
    instrumental = bool(hit.get("instrumental"))
    if not synced and not plain and not instrumental:
        return None
    return {"synced": synced, "plain": None if synced else plain, "instrumental": instrumental, "id": hit.get("id")}


def best_match(candidates: Any, artist: str, title: str, duration: float | None) -> dict[str, Any] | None:
    """From a search: the same song, at the same length (so timed lyrics still line up), preferring timed lyrics."""
    if not isinstance(candidates, list):
        return None
    fits = []
    for c in candidates:
        if not isinstance(c, dict) or _words(c.get("artistName")) != _words(artist) or _words(c.get("trackName")) != _words(title):
            continue
        length = c.get("duration")
        if duration and isinstance(length, (int, float)) and abs(length - duration) > DURATION_SLACK:
            continue
        fits.append((0 if c.get("syncedLyrics") else 1, abs(length - duration) if duration and isinstance(length, (int, float)) else 0.0, c))
    return min(fits, key=lambda f: (f[0], f[1]))[2] if fits else None


def lookup(artist: str | None, title: str | None, album: str | None = None, duration: float | None = None) -> dict[str, Any] | None:
    """The lyrics LRCLIB has for a song, {synced, plain, instrumental, id}, or None when it has none."""
    if not artist or not title:
        return None
    params: dict[str, Any] = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(round(duration))
    hit = _get("/get", params)
    if not isinstance(hit, dict):
        # Another edition of the song may be listed under another album: look for one of the same length.
        hit = best_match(_get("/search", {"artist_name": artist, "track_name": title}), artist, title, duration)
    return _shape(hit) if isinstance(hit, dict) else None


# ------------------------------------------------------------ what is kept

def cached(conn: sqlite3.Connection, track_id: int, key: str, now: float | None = None) -> dict[str, Any] | None:
    """The kept answer for this question, or None if there is none (or it is a "none" old enough to ask again)."""
    row = conn.execute("SELECT * FROM lyrics_cache WHERE track_id = ?", (track_id,)).fetchone()
    if row is None or row["query_key"] != key:
        return None
    if row["status"] == "none" and (now or time.time()) - row["fetched_at"] > NONE_KEPT_FOR:
        return None
    return {
        "status": row["status"], "synced": json.loads(row["synced"]) if row["synced"] else None,
        "plain": row["plain"], "instrumental": bool(row["instrumental"]),
    }


def remember(conn: sqlite3.Connection, track_id: int, key: str, found: dict[str, Any] | None, now: float | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO lyrics_cache (track_id, query_key, status, synced, plain, instrumental, source_id, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            track_id, key, "found" if found else "none", json.dumps(found["synced"]) if found and found["synced"] else None,
            found["plain"] if found else None, 1 if found and found["instrumental"] else 0, found["id"] if found else None, int(now or time.time()),
        ),
    )
    conn.commit()


_trouble: dict[int, float] = {}  # track id -> when its last lookup failed, so a closed-and-reopened tab does not ask again at once
_trouble_lock = threading.Lock()


def resolve(conn: sqlite3.Connection, track: sqlite3.Row | dict[str, Any], *, fetch: bool, force: bool = False, now: float | None = None) -> dict[str, Any]:
    """What is known online about a song that has no lyrics of its own: the kept answer, or (when `fetch`) a new one.

    Returns {"state": "found" | "none" | "off" | "unreachable", "synced", "plain", "instrumental", "message"}:
    "off" is a song nobody has asked about while lookups are off; "none" is a song LRCLIB does not have; "unreachable"
    is trouble (not kept). `force` is a person pressing Look up: it asks again about a "none" and about earlier trouble.
    """
    now = now or time.time()
    track_id = track["id"]
    key = query_key(track["artist"], track["title"], track["album"], track["duration"])
    kept = cached(conn, track_id, key, now)
    if kept is not None and not (force and kept["status"] == "none"):
        return {"state": kept["status"], **{k: kept[k] for k in ("synced", "plain", "instrumental")}}
    if not fetch:
        return {"state": "off"}
    with _trouble_lock:
        failed_at = _trouble.get(track_id)
    if not force and failed_at is not None and now - failed_at < RETRY_AFTER_TROUBLE:
        return {"state": "unreachable", "message": "LRCLIB could not be reached a moment ago. Try again with Look up."}
    try:
        found = lookup(track["artist"], track["title"], track["album"], track["duration"])
    except LrclibError as exc:
        with _trouble_lock:
            _trouble[track_id] = now
        return {"state": "unreachable", "message": str(exc)}
    with _trouble_lock:
        _trouble.pop(track_id, None)
    remember(conn, track_id, key, found, now)
    if found is None:
        return {"state": "none"}
    return {"state": "found", "synced": found["synced"], "plain": found["plain"], "instrumental": found["instrumental"]}
