"""Finding podcasts: Apple's podcast directory (the iTunes Search API), which is free, needs no account and lists the feed address of each show.

Nothing here runs until the person looks for a podcast. What goes out is the search words (and the store's country) and a plain User-Agent.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode

from musictoolkit.net import guard

MAX_ANSWER = 2_000_000


class DirectoryError(Exception):
    """The directory could not be asked; the message says why in words a person can read."""


def _base() -> str:
    return (os.environ.get("MTK_ITUNES_URL") or "https://itunes.apple.com").rstrip("/")  # the variable is for tests and the packaged-app check


def _show(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    try:
        feed = guard.check_url(entry["feedUrl"]) if entry.get("feedUrl") else None
    except guard.UnsafeURL:
        feed = None
    title = " ".join(str(entry.get("collectionName") or entry.get("trackName") or "").split())
    if not feed or not title:
        return None
    art = next((a for a in (entry.get("artworkUrl600"), entry.get("artworkUrl100"), entry.get("artworkUrl60")) if isinstance(a, str) and a.startswith("http")), None)
    return {
        "itunes_id": str(entry.get("collectionId") or "") or None,
        "title": title[:300],
        "author": " ".join(str(entry.get("artistName") or "").split())[:200],
        "feed_url": feed,
        "art_url": art,
        "genre": str(entry.get("primaryGenreName") or "") or None,
        "episodes": int(entry["trackCount"]) if str(entry.get("trackCount") or "").isdigit() else None,
    }


def search(term: str, *, country: str = "US", limit: int = 25) -> list[dict[str, Any]]:
    term = " ".join(term.split())[:100]
    if not term:
        return []
    query = urlencode({"media": "podcast", "entity": "podcast", "term": term, "limit": max(1, min(limit, 50)), "country": (country or "US")[:2].upper()})
    try:
        got = guard.fetch(f"{_base()}/search?{query}", max_bytes=MAX_ANSWER, deadline=20.0, headers={"Accept": "application/json"})
        data = json.loads(got.content)
    except guard.FetchError as exc:
        raise DirectoryError(f"The podcast directory could not be reached ({exc})") from exc
    except ValueError as exc:
        raise DirectoryError("The podcast directory sent an answer that could not be read.") from exc
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise DirectoryError("The podcast directory sent an answer that could not be read.")
    return [show for show in (_show(entry) for entry in results) if show]
