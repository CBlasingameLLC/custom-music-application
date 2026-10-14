"""Following a podcast and asking its feed for new episodes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from musictoolkit.net import guard
from musictoolkit.podcasts import store
from musictoolkit.podcasts.feed import FeedError, parse_feed

MAX_FEED_BYTES = 30_000_000
FEED_DEADLINE = 45.0
REFRESH_EVERY = timedelta(hours=6)
ACCEPT = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"


class PodcastError(Exception):
    """The podcast cannot be followed or checked; the message says why in words a person can read."""


def _fetch(feed_url: str, *, etag: str | None = None, last_modified: str | None = None) -> guard.Fetched:
    headers = {"Accept": ACCEPT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        return guard.fetch(feed_url, max_bytes=MAX_FEED_BYTES, deadline=FEED_DEADLINE, headers=headers, accepted_status=(200, 304))
    except guard.FetchError as exc:
        raise PodcastError(str(exc)) from exc


def follow(conn: sqlite3.Connection, feed_url: str, *, itunes_id: str | None = None) -> dict[str, Any]:
    """Read the feed at `feed_url` and follow the podcast (its episodes already out are kept, unheard). Returns the podcast."""
    try:
        url = guard.check_url(feed_url)
    except guard.UnsafeURL as exc:
        raise PodcastError(str(exc)) from exc
    got = _fetch(url)
    try:
        feed = parse_feed(got.content)
    except FeedError as exc:
        raise PodcastError(str(exc)) from exc
    return store.subscribe(conn, feed, url, itunes_id=itunes_id, etag=got.headers.get("etag"), last_modified=got.headers.get("last-modified"))


def refresh(conn: sqlite3.Connection, podcast_id: int, *, force: bool = False) -> dict[str, Any]:
    """Ask one podcast's feed whether it has anything new. Never raises: the answer says how it went
    ({"status": "updated" | "unchanged" | "error", "new": episodes new here, "message": why when it went wrong})."""
    row = conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    if row is None:
        return {"id": podcast_id, "title": None, "status": "error", "new": 0, "message": "That podcast is not followed."}
    answer = {"id": podcast_id, "title": row["title"], "status": "unchanged", "new": 0, "message": ""}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        got = _fetch(row["feed_url"], etag=None if force else row["etag"], last_modified=None if force else row["last_modified"])
        if got.status == 304:
            conn.execute("UPDATE podcasts SET last_checked_at = ?, error = NULL WHERE id = ?", (now, podcast_id))
            conn.commit()
            return answer
        feed = parse_feed(got.content)
    except (PodcastError, FeedError) as exc:
        conn.execute("UPDATE podcasts SET last_checked_at = ?, error = ? WHERE id = ?", (now, str(exc)[:300], podcast_id))
        conn.commit()
        return {**answer, "status": "error", "message": str(exc)}
    fresh = store.upsert_episodes(conn, podcast_id, feed.episodes)
    conn.execute(
        "UPDATE podcasts SET title=?, author=?, description=?, site_url=?, art_url=COALESCE(?, art_url), etag=?, last_modified=?, last_checked_at=?, error=NULL WHERE id=?",
        (feed.title, feed.author, feed.description, feed.site_url, feed.art_url, got.headers.get("etag"), got.headers.get("last-modified"), now, podcast_id),
    )
    conn.commit()
    return {**answer, "title": feed.title, "status": "updated", "new": fresh}


def refresh_all(conn: sqlite3.Connection, *, only: int | None = None, progress: Callable[[int, int, str], None] | None = None, check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Check every podcast followed (or just `only`). `progress(done, total, title)` and `check()` (which raises to cancel) are called between feeds."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM podcasts " + ("WHERE id = ? " if only else "") + "ORDER BY title COLLATE NOCASE", ((only,) if only else ()))]
    totals = {"checked": 0, "new": 0, "errors": 0, "messages": []}
    for done, podcast_id in enumerate(ids):
        if check:
            check()
        if progress:
            title = conn.execute("SELECT title FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
            progress(done, len(ids), title["title"] if title else "")
        result = refresh(conn, podcast_id)
        totals["checked"] += 1
        totals["new"] += result["new"]
        if result["status"] == "error":
            totals["errors"] += 1
            totals["messages"].append(f"{result['title']}: {result['message']}")
    if progress:
        progress(len(ids), len(ids), "")
    return totals


def due(oldest_check: str | None, now: datetime) -> bool:
    """Has it been long enough since the least recently checked podcast was checked?"""
    if not oldest_check:
        return True
    try:
        last = datetime.fromisoformat(oldest_check)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= REFRESH_EVERY
