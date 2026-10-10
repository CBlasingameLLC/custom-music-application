"""Mixes: ready-made queues from the library and the listening history, and song radio.

A mix is a question asked of the library ("what have I been playing a lot this month?", "what used to be a
favourite that I have not heard in half a year?"). Mixes that pick at random pick the same songs all day, so a
card on Home keeps its cover and opening it twice gives the same list; tomorrow it is a new draw.
"""

from __future__ import annotations

import base64
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from musictoolkit.web.queries import HISTORY_JOIN, tracks_by_ids

MIN_TRACKS = 8  # fewer than this is not worth a card
DEFAULT_SIZE = 50
MAX_SIZE = 200
RECENT_DAYS = 30
FORGOTTEN_DAYS = 180
LOVED_FORGOTTEN_DAYS = 90
GENRES = 6
FROM = f"FROM tracks t {HISTORY_JOIN}"


@dataclass
class Mix:
    id: str
    title: str
    subtitle: str
    where: str
    params: list[Any] = field(default_factory=list)
    order: str | None = None  # None: a random draw; otherwise an SQL ORDER BY
    order_params: list[Any] = field(default_factory=list)


def daily_seed(now: datetime) -> int:
    return int(now.timestamp() // 86400)


def _since(now: datetime, days: int) -> int:
    return int((now - timedelta(days=days)).timestamp())


def _month_days(now: datetime, offset_minutes: int, spread: int = 3) -> list[str]:
    """Today's month-day and the `spread` days either side of it, as 'MM-DD' in the viewer's calendar."""
    today = now.astimezone(timezone(timedelta(minutes=offset_minutes))).date()
    return sorted({(today + timedelta(days=d)).strftime("%m-%d") for d in range(-spread, spread + 1)})


def _specs(conn: sqlite3.Connection, now: datetime, offset_minutes: int) -> list[Mix]:
    recent = _since(now, RECENT_DAYS)
    this_year = int(datetime(now.year, 1, 1, tzinfo=timezone.utc).timestamp())
    days = _month_days(now, offset_minutes)
    specs = [
        Mix(
            "on-repeat", "On repeat", f"What you have kept coming back to in the last {RECENT_DAYS} days",
            "t.id IN (SELECT track_id FROM play_history WHERE track_id IS NOT NULL AND played_at_epoch >= ? GROUP BY track_id HAVING COUNT(*) >= 2)",
            [recent],
            "(SELECT COUNT(*) FROM play_history ph WHERE ph.track_id = t.id AND ph.played_at_epoch >= ?) DESC, h.last_played DESC, t.id",
            [recent],
        ),
        Mix(
            "rediscover", "Rediscover", "Songs you loved and have not heard in a while",
            "((COALESCE(h.plays, 0) >= 3 AND (h.last_played IS NULL OR h.last_played < ?)) OR (t.favorite = 1 AND h.last_played < ?))",
            [_since(now, FORGOTTEN_DAYS), _since(now, LOVED_FORGOTTEN_DAYS)],
        ),
        Mix(
            "on-this-day", "On this day", "What you were playing around this date in earlier years",
            f"t.id IN (SELECT track_id FROM play_history WHERE track_id IS NOT NULL AND played_at_epoch < ? "
            f"AND strftime('%m-%d', played_at_epoch + ?, 'unixepoch') IN ({', '.join('?' * len(days))}))",
            [this_year, offset_minutes * 60, *days],
            "(SELECT COUNT(*) FROM play_history ph WHERE ph.track_id = t.id) DESC, t.id", [],
        ),
        Mix(
            "new-arrivals", "New in your library", f"Added in the last {RECENT_DAYS} days",
            "julianday(t.date_added) >= julianday(?)", [(now - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")],
            "t.date_added DESC, t.id",
        ),
        Mix("unheard", "Never played", "Songs in your library you have not listened to yet", "COALESCE(h.plays, 0) = 0"),
        Mix("favorites", "Your favorites", "The songs you have starred, shuffled", "t.favorite = 1"),
    ]
    for row in conn.execute(
        "SELECT (t.year / 10) * 10 AS decade, COUNT(*) AS n FROM tracks t WHERE t.is_missing = 0 AND t.year >= 1900 "
        "GROUP BY decade HAVING n >= ? ORDER BY n DESC", (MIN_TRACKS,)
    ).fetchall()[:4]:
        decade = int(row["decade"])
        specs.append(Mix(f"decade-{decade}", f"The {decade}s", "From your library, shuffled", "t.year >= ? AND t.year < ?", [decade, decade + 10]))
    for row in conn.execute(
        "SELECT t.genre AS genre, COUNT(*) AS n FROM tracks t WHERE t.is_missing = 0 AND TRIM(COALESCE(t.genre, '')) <> '' "
        "GROUP BY LOWER(t.genre) HAVING n >= ? ORDER BY n DESC LIMIT ?", (MIN_TRACKS, GENRES)
    ).fetchall():
        specs.append(Mix(genre_id(row["genre"]), row["genre"], "A shuffle of the genre", "LOWER(t.genre) = LOWER(?)", [row["genre"]]))
    return specs


def _count(conn: sqlite3.Connection, spec: Mix) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n {FROM} WHERE t.is_missing = 0 AND {spec.where}", spec.params).fetchone()["n"]


def _ids(conn: sqlite3.Connection, spec: Mix, size: int, seed: int) -> list[int]:
    base = f"SELECT t.id {FROM} WHERE t.is_missing = 0 AND {spec.where}"
    if spec.order:
        rows = conn.execute(f"{base} ORDER BY {spec.order} LIMIT ?", [*spec.params, *spec.order_params, size]).fetchall()
        return [r["id"] for r in rows]
    everything = [r["id"] for r in conn.execute(f"{base} ORDER BY t.id", spec.params).fetchall()]
    return random.Random(f"{spec.id}:{seed}").sample(everything, min(size, len(everything)))


def available(conn: sqlite3.Connection, now: datetime | None = None, offset_minutes: int = 0, size: int = DEFAULT_SIZE, minimum: int = MIN_TRACKS) -> list[dict[str, Any]]:
    """The mixes worth showing today, each with how many songs it holds and a song to take the cover from."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    seed = daily_seed(now)
    found = []
    for spec in _specs(conn, now, offset_minutes):
        count = _count(conn, spec)
        if count < minimum:
            continue
        cover = _ids(conn, spec, 1, seed)
        found.append({"id": spec.id, "title": spec.title, "subtitle": spec.subtitle, "count": min(count, size), "cover_track_id": cover[0] if cover else None})
    return found


def build(conn: sqlite3.Connection, mix_id: str, now: datetime | None = None, offset_minutes: int = 0, size: int = DEFAULT_SIZE, seed: int | None = None) -> dict[str, Any] | None:
    """One mix with its songs in play order, or None if there is no such mix (or it is empty today)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    size = max(1, min(int(size), MAX_SIZE))
    chosen = next((s for s in _specs(conn, now, offset_minutes) if s.id == mix_id), None)
    if chosen is None:
        return None
    ids = _ids(conn, chosen, size, daily_seed(now) if seed is None else seed)
    if not ids:
        return None
    return {"id": chosen.id, "title": chosen.title, "subtitle": chosen.subtitle, "tracks": tracks_by_ids(conn, ids)}


def genre_id(genre: str) -> str:
    """A mix id that is safe in a URL whatever the genre is called (letters, digits, - and _ only)."""
    return "genre-" + base64.urlsafe_b64encode(genre.encode("utf-8")).decode("ascii").rstrip("=")


def genre_of(mix_id: str) -> str | None:
    if not mix_id.startswith("genre-"):
        return None
    encoded = mix_id[len("genre-"):]
    try:
        text = base64.b64decode((encoded + "=" * (-len(encoded) % 4)).replace("-", "+").replace("_", "/"), validate=True).decode("utf-8")
    except ValueError:  # not base64, or not text
        return None
    return text or None


# ------------------------------------------------------------------------------------------- song radio

CO_LISTEN_SECONDS = 20 * 60  # songs played within twenty minutes of the seed belong to the same sitting


def radio(conn: sqlite3.Connection, track_id: int, size: int = DEFAULT_SIZE, seed: int | None = None) -> list[dict[str, Any]]:
    """Songs that go with one song: the same sitting in your history, the same genre and era, the same artist (a few),
    and what you tend to like. Returns the seed first, then the rest in an order that does not repeat an artist back to back."""
    size = max(2, min(int(size), MAX_SIZE))
    seed_row = conn.execute(
        "SELECT id, artist, album_artist, genre, year FROM tracks WHERE id = ? AND is_missing = 0", (track_id,)
    ).fetchone()
    if seed_row is None:
        return []
    seed_artist = (seed_row["artist"] or seed_row["album_artist"] or "").strip().lower()
    seed_genre = (seed_row["genre"] or "").strip().lower()

    together: dict[int, int] = {}
    for row in conn.execute(
        "SELECT o.track_id AS id, COUNT(*) AS n FROM play_history s JOIN play_history o ON o.track_id IS NOT NULL AND o.track_id <> s.track_id "
        "AND o.played_at_epoch BETWEEN s.played_at_epoch - ? AND s.played_at_epoch + ? WHERE s.track_id = ? GROUP BY o.track_id",
        (CO_LISTEN_SECONDS, CO_LISTEN_SECONDS, track_id),
    ):
        together[row["id"]] = row["n"]
    most_together = max(together.values(), default=0)

    rng = random.Random(f"radio:{track_id}:{seed}")
    scored: list[tuple[float, int, str]] = []
    for row in conn.execute(
        "SELECT t.id, t.artist, t.album_artist, t.genre, t.year, t.favorite, COALESCE(t.rating, 0) AS rating, COALESCE(h.plays, 0) AS plays "
        f"{FROM} WHERE t.is_missing = 0 AND t.id <> ?", (track_id,)
    ).fetchall():
        artist = (row["artist"] or row["album_artist"] or "").strip().lower()
        genre = (row["genre"] or "").strip().lower()
        score = 0.0
        if row["id"] in together:
            score += 4 * together[row["id"]] / most_together + 2
        if seed_genre and genre == seed_genre:
            score += 3
        if seed_row["year"] and row["year"]:
            gap = abs(int(seed_row["year"]) - int(row["year"]))
            score += 2 if gap <= 3 else 1 if gap <= 8 else 0
        if seed_artist and artist == seed_artist:
            score += 2.5
        if row["favorite"] or row["rating"] >= 4:
            score += 1
        if score > 0:
            scored.append((score + rng.random() * 0.5, row["id"], artist))
    scored.sort(reverse=True)

    # Spread the artists: no more than a third of the radio from one, and never the same artist twice in a row.
    per_artist: dict[str, int] = {}
    picked: list[tuple[int, str]] = []
    cap = max(2, size // 3)
    for _, candidate, artist in scored:
        if per_artist.get(artist, 0) >= cap:
            continue
        per_artist[artist] = per_artist.get(artist, 0) + 1
        picked.append((candidate, artist))
        if len(picked) >= size - 1:
            break
    ordered: list[int] = [track_id]
    last_artist = seed_artist
    waiting = list(picked)
    while waiting:
        index = next((i for i, (_, artist) in enumerate(waiting) if artist != last_artist), 0)
        candidate, artist = waiting.pop(index)
        ordered.append(candidate)
        last_artist = artist
    return tracks_by_ids(conn, ordered)
