"""Finding stations: the Radio Browser directory (radio-browser.info), a free community-run list of internet radio stations.

Nothing here runs until the person opens Radio and looks. What goes out is the search (a name, a tag, a country) and a plain User-Agent.
Answers are kept in memory for a few minutes so that paging back and forth does not ask again. Stations the directory itself marks as broken
are not asked for, and ones that use HLS are dropped, because the player (a browser engine) cannot play them.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlencode

from musictoolkit.net import guard

DEFAULT_BASES = [
    "https://all.api.radio-browser.info/json",
    "https://de1.api.radio-browser.info/json",
    "https://nl1.api.radio-browser.info/json",
]
MAX_ANSWER = 4_000_000
CACHE_SECONDS = 300
CACHE_ENTRIES = 200
PAGE = 40

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


class DirectoryError(Exception):
    """The directory could not be asked; the message says why in words a person can read."""


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _bases() -> list[str]:
    override = os.environ.get("MTK_RADIO_BROWSER_URL")  # for tests and the packaged-app check
    return [override.rstrip("/")] if override else DEFAULT_BASES


def _get(path: str, params: dict[str, Any]) -> Any:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    key = f"{path}?{query}"
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    last = "The directory could not be reached."
    for base in _bases():
        try:
            got = guard.fetch(f"{base}{path}" + (f"?{query}" if query else ""), max_bytes=MAX_ANSWER, deadline=20.0, headers={"Accept": "application/json"})
            data = json.loads(got.content)
        except guard.FetchError as exc:
            last = f"The station directory could not be reached ({exc})"
            continue
        except ValueError:
            last = "The station directory sent an answer that could not be read."
            continue
        with _lock:
            if len(_cache) >= CACHE_ENTRIES:
                _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
            _cache[key] = (time.monotonic(), data)
        return data
    raise DirectoryError(last)


def _clean_link(value: Any) -> str | None:
    try:
        return guard.check_url(value) if isinstance(value, str) and value.strip() else None
    except guard.UnsafeURL:
        return None


def station_from(raw: Any) -> dict[str, Any] | None:
    """One directory entry as the station we keep, or None when it is unusable (no name, no web address, HLS, not working)."""
    if not isinstance(raw, dict) or raw.get("hls") or raw.get("lastcheckok") == 0:
        return None
    url = _clean_link(raw.get("url_resolved")) or _clean_link(raw.get("url"))
    name = " ".join(str(raw.get("name") or "").split())
    if not url or not name:
        return None
    tags = [t.strip().lower() for t in str(raw.get("tags") or "").split(",") if t.strip()]
    return {
        "source": "radio-browser",
        "source_id": str(raw.get("stationuuid") or "") or None,
        "name": name[:200],
        "stream_url": url,
        "homepage": _clean_link(raw.get("homepage")),
        "art_url": _clean_link(raw.get("favicon")),
        "tags": tags[:8],
        "country": str(raw.get("country") or "").strip() or None,
        "country_code": str(raw.get("countrycode") or "").strip().upper()[:2] or None,
        "language": str(raw.get("language") or "").strip() or None,
        "codec": str(raw.get("codec") or "").strip() or None,
        "bitrate": int(raw["bitrate"]) if str(raw.get("bitrate") or "").isdigit() and int(raw["bitrate"]) > 0 else None,
        "votes": int(raw["votes"]) if str(raw.get("votes") or "").lstrip("-").isdigit() else 0,
    }


ORDERS = {"popular": ("clickcount", "true"), "votes": ("votes", "true"), "name": ("name", "false"), "bitrate": ("bitrate", "true")}


def search(*, name: str = "", tag: str = "", country_code: str = "", language: str = "", order: str = "popular", offset: int = 0, limit: int = PAGE) -> dict[str, Any]:
    """{"items": [stations], "more": bool}. `more` says there may be another page (the directory gave as many as were asked for)."""
    sort, reverse = ORDERS.get(order, ORDERS["popular"])
    raw = _get(
        "/stations/search",
        {
            "name": name.strip()[:100], "tag": tag.strip()[:60], "countrycode": country_code.strip()[:2], "language": language.strip()[:40],
            "order": sort, "reverse": reverse, "hidebroken": "true", "limit": max(1, min(int(limit), 100)), "offset": max(0, int(offset)),
        },
    )
    if not isinstance(raw, list):
        raise DirectoryError("The station directory sent an answer that could not be read.")
    items = [s for s in (station_from(entry) for entry in raw) if s]
    return {"items": items, "more": len(raw) >= limit}


def tags(limit: int = 60) -> list[dict[str, Any]]:
    raw = _get("/tags", {"order": "stationcount", "reverse": "true", "hidebroken": "true", "limit": max(1, min(limit * 2, 200))})
    found = []
    for entry in raw if isinstance(raw, list) else []:
        name = str(entry.get("name") or "").strip().lower() if isinstance(entry, dict) else ""
        if name and len(name) <= 30 and not name.isdigit() and name.isprintable():
            found.append({"name": name, "count": int(entry.get("stationcount") or 0)})
    return found[:limit]


def countries(limit: int = 100) -> list[dict[str, Any]]:
    raw = _get("/countries", {"order": "stationcount", "reverse": "true", "hidebroken": "true", "limit": max(1, min(limit, 300))})
    found = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        name, code = str(entry.get("name") or "").strip(), str(entry.get("iso_3166_1") or "").strip().upper()
        if name and len(code) == 2:
            found.append({"name": name, "code": code, "count": int(entry.get("stationcount") or 0)})
    return found[:limit]


def click(source_id: str | None) -> None:
    """Tell the directory a station was played (it counts these to rank stations; its rules ask apps to). Best effort, never raises."""
    if not source_id or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", source_id):
        return
    try:
        guard.fetch(f"{_bases()[0]}/url/{source_id}", max_bytes=100_000, deadline=10.0)
    except guard.FetchError:
        pass
