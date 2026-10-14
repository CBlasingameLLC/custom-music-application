"""The podcasts the person follows and their episodes."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from musictoolkit.podcasts.feed import ParsedEpisode, ParsedFeed

PLAYED_WHEN_LEFT = 30.0  # seconds from the end: an episode listened to this far counts as heard
PLAYED_FRACTION = 0.97
NEW_WHEN_FOLLOWED = 3  # the latest episodes of a podcast that has just been followed count as new; its back catalogue does not
NEW_AT_MOST = 20  # and so many at most of what one check finds (a feed that suddenly lists its whole history is not 700 new episodes)
# "New episodes": not heard yet, and either came out since the podcast was followed or half heard.
NEW_WHERE = "e.played = 0 AND (e.is_new = 1 OR e.position_seconds > 0)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def podcast_json(row: sqlite3.Row, *, unplayed: int = 0, episodes: int = 0, latest: str | None = None, fresh: int = 0) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "author": row["author"] or "",
        "description": row["description"] or "",
        "site_url": row["site_url"],
        "feed_url": row["feed_url"],
        "has_art": bool(row["art_url"]),
        "subscribed_at": row["subscribed_at"],
        "last_checked_at": row["last_checked_at"],
        "error": row["error"],
        "unplayed": unplayed,
        "new": fresh,
        "episodes": episodes,
        "latest": latest,
    }


def episode_json(row: sqlite3.Row, podcast_title: str | None = None, podcast_has_art: bool | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": "episode",
        "podcast_id": row["podcast_id"],
        "podcast_title": podcast_title,
        "title": row["title"],
        "description": row["description"] or "",
        "published_at": row["published_at"],
        "duration": row["duration_seconds"],
        "audio_type": row["audio_type"],
        "has_art": bool(row["art_url"]) or bool(podcast_has_art),
        "season": row["season"],
        "episode": row["episode"],
        "position": row["position_seconds"],
        "played": bool(row["played"]),
        "played_at": row["played_at"],
        "new": not row["played"] and bool(row["is_new"] or row["position_seconds"] > 0),
    }


def upsert_episodes(conn: sqlite3.Connection, podcast_id: int, episodes: list[ParsedEpisode], *, mark_new: int = NEW_AT_MOST) -> int:
    """Keep these episodes; returns how many are not kept yet (the newest `mark_new` of those are flagged new). One already kept (same guid)
    has its details refreshed and keeps where listening stopped."""
    added: list[tuple[int, str | None]] = []
    for ep in episodes:
        values = (ep.title, ep.description, ep.published_at, ep.duration_seconds, ep.audio_url, ep.audio_type, ep.audio_bytes, ep.art_url, ep.season, ep.episode)
        updated = conn.execute(
            "UPDATE podcast_episodes SET title=?, description=?, published_at=?, duration_seconds=?, audio_url=?, audio_type=?, audio_bytes=?, art_url=?, season=?, episode=?"
            " WHERE podcast_id=? AND guid=?",
            (*values, podcast_id, ep.guid),
        ).rowcount
        if not updated:
            cursor = conn.execute(
                "INSERT INTO podcast_episodes (title, description, published_at, duration_seconds, audio_url, audio_type, audio_bytes, art_url, season, episode, podcast_id, guid)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (*values, podcast_id, ep.guid),
            )
            added.append((cursor.lastrowid, ep.published_at))
    if added and mark_new > 0:
        newest = sorted(added, key=lambda e: (e[1] is not None, e[1] or "", e[0]), reverse=True)[:mark_new]  # dated ones first, newest first
        conn.execute(f"UPDATE podcast_episodes SET is_new = 1 WHERE id IN ({','.join('?' * len(newest))})", [e[0] for e in newest])
    return len(added)


def subscribe(conn: sqlite3.Connection, feed: ParsedFeed, feed_url: str, *, itunes_id: str | None = None, etag: str | None = None, last_modified: str | None = None) -> dict[str, Any]:
    """Follow a podcast (or refresh the details of one already followed) and keep its episodes; the ones already out are marked unheard."""
    now = _now()
    row = conn.execute("SELECT id FROM podcasts WHERE feed_url = ?", (feed_url,)).fetchone()
    if row is None:
        podcast_id = conn.execute(
            "INSERT INTO podcasts (feed_url, title, author, description, site_url, art_url, itunes_id, subscribed_at, last_checked_at, etag, last_modified)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (feed_url, feed.title, feed.author, feed.description, feed.site_url, feed.art_url, itunes_id, now, now, etag, last_modified),
        ).lastrowid
    else:
        podcast_id = row["id"]
        conn.execute(
            "UPDATE podcasts SET title=?, author=?, description=?, site_url=?, art_url=COALESCE(?, art_url), itunes_id=COALESCE(?, itunes_id), last_checked_at=?, etag=?, last_modified=?, error=NULL WHERE id=?",
            (feed.title, feed.author, feed.description, feed.site_url, feed.art_url, itunes_id, now, etag, last_modified, podcast_id),
        )
    upsert_episodes(conn, podcast_id, feed.episodes, mark_new=NEW_WHEN_FOLLOWED if row is None else NEW_AT_MOST)
    conn.commit()
    return get_podcast(conn, podcast_id)  # type: ignore[return-value]


def _counts(conn: sqlite3.Connection, podcast_id: int) -> tuple[int, int, int, str | None]:
    row = conn.execute(
        f"SELECT COUNT(*) AS n, SUM(CASE WHEN e.played = 0 THEN 1 ELSE 0 END) AS unplayed, SUM(CASE WHEN {NEW_WHERE} THEN 1 ELSE 0 END) AS fresh,"
        " MAX(e.published_at) AS latest FROM podcast_episodes e WHERE e.podcast_id = ?", (podcast_id,)
    ).fetchone()
    return row["n"], row["unplayed"] or 0, row["fresh"] or 0, row["latest"]


def get_podcast(conn: sqlite3.Connection, podcast_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    if row is None:
        return None
    n, unplayed, fresh, latest = _counts(conn, podcast_id)
    return podcast_json(row, unplayed=unplayed, episodes=n, latest=latest, fresh=fresh)


def list_podcasts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The podcasts followed, the one with the newest episode first."""
    rows = conn.execute(
        f"SELECT p.*, COUNT(e.id) AS n, SUM(CASE WHEN e.played = 0 THEN 1 ELSE 0 END) AS unplayed, SUM(CASE WHEN {NEW_WHERE} THEN 1 ELSE 0 END) AS fresh,"
        " MAX(e.published_at) AS latest"
        " FROM podcasts p LEFT JOIN podcast_episodes e ON e.podcast_id = p.id GROUP BY p.id ORDER BY latest IS NULL, latest DESC, p.title COLLATE NOCASE"
    ).fetchall()
    return [podcast_json(r, unplayed=r["unplayed"] or 0, episodes=r["n"], latest=r["latest"], fresh=r["fresh"] or 0) for r in rows]


def unsubscribe(conn: sqlite3.Connection, podcast_id: int) -> bool:
    cursor = conn.execute("DELETE FROM podcasts WHERE id = ?", (podcast_id,))
    conn.commit()
    return cursor.rowcount > 0


def list_episodes(conn: sqlite3.Connection, podcast_id: int, *, unplayed_only: bool = False, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    where = "podcast_id = ?" + (" AND played = 0" if unplayed_only else "")
    total = conn.execute(f"SELECT COUNT(*) FROM podcast_episodes WHERE {where}", (podcast_id,)).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM podcast_episodes WHERE {where} ORDER BY published_at IS NULL, published_at DESC, id DESC LIMIT ? OFFSET ?", (podcast_id, limit, offset)
    ).fetchall()
    podcast = conn.execute("SELECT title, art_url FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    title, has_art = (podcast["title"], bool(podcast["art_url"])) if podcast else (None, False)
    return {"items": [episode_json(r, title, has_art) for r in rows], "total": total}


def new_episodes(conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """The new episodes of every podcast followed (not heard yet, and out since it was followed, or half heard), newest first."""
    total = conn.execute(f"SELECT COUNT(*) FROM podcast_episodes e WHERE {NEW_WHERE}").fetchone()[0]
    rows = conn.execute(
        "SELECT e.*, p.title AS podcast_title, p.art_url AS podcast_art FROM podcast_episodes e JOIN podcasts p ON p.id = e.podcast_id"
        f" WHERE {NEW_WHERE} ORDER BY e.published_at IS NULL, e.published_at DESC, e.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {"items": [episode_json(r, r["podcast_title"], bool(r["podcast_art"])) for r in rows], "total": total}


def get_episode(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT e.*, p.title AS podcast_title, p.art_url AS podcast_art FROM podcast_episodes e JOIN podcasts p ON p.id = e.podcast_id WHERE e.id = ?", (episode_id,)
    ).fetchone()
    return episode_json(row, row["podcast_title"], bool(row["podcast_art"])) if row else None


def episode_row(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT e.*, p.title AS podcast_title, p.art_url AS podcast_art FROM podcast_episodes e JOIN podcasts p ON p.id = e.podcast_id WHERE e.id = ?", (episode_id,)
    ).fetchone()


def set_progress(conn: sqlite3.Connection, episode_id: int, position: float, duration: float | None = None) -> dict[str, Any] | None:
    """Where listening to an episode has got to. Near the end it counts as heard (and the place is forgotten, so it starts over next time)."""
    row = conn.execute("SELECT duration_seconds, played FROM podcast_episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        return None
    position = max(0.0, float(position))
    length = float(duration) if duration and duration > 0 else row["duration_seconds"]
    if length and (position >= length - PLAYED_WHEN_LEFT or position >= length * PLAYED_FRACTION):
        conn.execute("UPDATE podcast_episodes SET position_seconds = 0, played = 1, played_at = COALESCE(played_at, ?) WHERE id = ?", (_now(), episode_id))
    else:
        conn.execute("UPDATE podcast_episodes SET position_seconds = ? WHERE id = ?", (position, episode_id))
    if duration and duration > 0 and not row["duration_seconds"]:
        conn.execute("UPDATE podcast_episodes SET duration_seconds = ? WHERE id = ?", (float(duration), episode_id))
    conn.commit()
    return get_episode(conn, episode_id)


def set_played(conn: sqlite3.Connection, episode_id: int, played: bool) -> dict[str, Any] | None:
    if conn.execute("SELECT 1 FROM podcast_episodes WHERE id = ?", (episode_id,)).fetchone() is None:
        return None
    if played:
        conn.execute("UPDATE podcast_episodes SET played = 1, position_seconds = 0, played_at = COALESCE(played_at, ?) WHERE id = ?", (_now(), episode_id))
    else:  # marked unheard: it is back among the new episodes
        conn.execute("UPDATE podcast_episodes SET played = 0, is_new = 1, played_at = NULL WHERE id = ?", (episode_id,))
    conn.commit()
    return get_episode(conn, episode_id)


def mark_all_played(conn: sqlite3.Connection, podcast_id: int) -> int:
    cursor = conn.execute(
        "UPDATE podcast_episodes SET played = 1, position_seconds = 0, played_at = COALESCE(played_at, ?) WHERE podcast_id = ? AND played = 0", (_now(), podcast_id)
    )
    conn.commit()
    return cursor.rowcount
