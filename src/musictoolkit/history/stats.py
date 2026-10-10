"""Listening statistics from the play history: how much, what, and when.

Every play in `play_history` counts, whether it came from the in-app player or an imported Spotify history, and
whether or not the song is in the library any more (the names are copied into each play). Times are the viewer's:
the caller passes the offset of their time zone and plays are bucketed by local day, hour and weekday. A fixed
offset is exact except for plays on the far side of a daylight saving change, where an hour may land in the
neighbouring bucket; that does not matter for what these numbers are used for.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

MAX_OFFSET_MINUTES = 14 * 60
RANGE_PATTERN = re.compile(r"^(all|year:\d{4}|month:\d{4}-(0[1-9]|1[0-2])|days:\d{1,5})$")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_LIMIT = 50


class BadRange(ValueError):
    """The requested period is not one of: all, year:2025, month:2025-06, days:30."""


def clamp_offset(minutes: int) -> int:
    """The viewer's offset from UTC in minutes (east positive), kept to what exists on Earth."""
    return max(-MAX_OFFSET_MINUTES, min(MAX_OFFSET_MINUTES, int(minutes)))


def resolve_range(spec: str, offset_minutes: int, now: datetime | None = None) -> tuple[int, int, str]:
    """(start epoch, end epoch exclusive, title) for a period, in the viewer's time zone."""
    if not RANGE_PATTERN.match(spec or ""):
        raise BadRange("period must be all, year:2025, month:2025-06 or days:30")
    zone = timezone(timedelta(minutes=clamp_offset(offset_minutes)))
    now = (now or datetime.now(timezone.utc)).astimezone(zone)

    def at(year: int, month: int = 1) -> int:
        return int(datetime(year, month, 1, tzinfo=zone).timestamp())

    kind, _, value = spec.partition(":")
    if kind == "all":
        return 0, int(now.timestamp()) + 86400, "All time"
    if kind == "year":
        year = int(value)
        return at(year), at(year + 1), str(year)
    if kind == "month":
        year, month = (int(part) for part in value.split("-"))
        end = at(year + 1) if month == 12 else at(year, month + 1)
        return at(year, month), end, datetime(year, month, 1).strftime("%B %Y")
    days = max(1, int(value))
    return int((now - timedelta(days=days)).timestamp()), int(now.timestamp()) + 1, f"The last {days} days"


# One row per play in the period, with the names the play carries (or the song's tags if it carries none).
PLAYS = """
WITH p AS (
  SELECT h.id AS id, h.track_id AS track_id, h.played_at_epoch AS at, h.played_at_epoch + :off AS ts,
         COALESCE(h.ms_played, 0) AS ms,
         COALESCE(NULLIF(TRIM(h.raw_artist_name), ''), NULLIF(TRIM(t.artist), '')) AS artist,
         COALESCE(NULLIF(TRIM(h.raw_track_name), ''), NULLIF(TRIM(t.title), '')) AS title,
         COALESCE(NULLIF(TRIM(h.raw_album_name), ''), NULLIF(TRIM(t.album), '')) AS album,
         NULLIF(TRIM(t.genre), '') AS genre
  FROM play_history h LEFT JOIN tracks t ON t.id = h.track_id
  WHERE h.played_at_epoch >= :start AND h.played_at_epoch < :end
)
"""


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_LIMIT))


def years(conn: sqlite3.Connection, offset_minutes: int) -> list[dict[str, int]]:
    """The calendar years with at least one play, newest first."""
    rows = conn.execute(
        "SELECT strftime('%Y', played_at_epoch + ?, 'unixepoch') AS year, COUNT(*) AS plays FROM play_history "
        "GROUP BY year ORDER BY year DESC",
        (clamp_offset(offset_minutes) * 60,),
    ).fetchall()
    return [{"year": int(r["year"]), "plays": r["plays"]} for r in rows]


def _longest_streak(days: list[str]) -> dict[str, Any]:
    best_len, best_end, run_len, previous = 0, None, 0, None
    for text in days:
        day = date.fromisoformat(text)
        run_len = run_len + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        if run_len > best_len:
            best_len, best_end = run_len, day
        previous = day
    if not best_len:
        return {"days": 0, "from": None, "to": None}
    return {"days": best_len, "from": (best_end - timedelta(days=best_len - 1)).isoformat(), "to": best_end.isoformat()}


def _month_keys(first: str, last: str) -> list[str]:
    year, month = (int(part) for part in first.split("-"))
    end_year, end_month = (int(part) for part in last.split("-"))
    keys = []
    while (year, month) <= (end_year, end_month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def overview(conn: sqlite3.Connection, spec: str = "all", offset_minutes: int = 0, limit: int = 10, now: datetime | None = None) -> dict[str, Any]:
    """Everything the Stats screen shows for one period."""
    offset = clamp_offset(offset_minutes)
    start, end, title = resolve_range(spec, offset, now)
    limit = _limit(limit)
    args = {"off": offset * 60, "start": start, "end": end}

    def rows(sql: str, extra: dict[str, Any] | None = None) -> list[sqlite3.Row]:
        return conn.execute(PLAYS + sql, {**args, **(extra or {})}).fetchall()

    totals = rows(
        "SELECT COUNT(*) AS plays, COALESCE(SUM(ms), 0) AS ms, COUNT(DISTINCT LOWER(artist)) AS artists, "
        "COUNT(DISTINCT LOWER(artist) || '|' || LOWER(title)) AS tracks, "
        "COUNT(DISTINCT date(ts, 'unixepoch')) AS days, MIN(at) AS first, MAX(at) AS last FROM p"
    )[0]
    result: dict[str, Any] = {
        "range": spec, "title": title, "start": start, "end": end, "offset_minutes": offset,
        "totals": {
            "plays": totals["plays"], "minutes": round(totals["ms"] / 60000), "artists": totals["artists"],
            "tracks": totals["tracks"], "active_days": totals["days"], "first_play": totals["first"], "last_play": totals["last"],
        },
    }
    if not totals["plays"]:
        return {**result, "empty": True, "top_artists": [], "top_tracks": [], "top_albums": [], "top_genres": [], "by_month": [],
                "by_hour": [0] * 24, "by_weekday": [0] * 7, "by_day": [], "streak": _longest_streak([]), "discoveries": {"count": 0, "items": []},
                "month_favorites": []}

    result["empty"] = False
    result["top_artists"] = [
        {"name": r["name"], "plays": r["plays"], "minutes": round(r["ms"] / 60000), "track_id": r["track_id"]}
        for r in rows(
            "SELECT MIN(artist) AS name, COUNT(*) AS plays, SUM(ms) AS ms, MAX(track_id) AS track_id FROM p WHERE artist IS NOT NULL "
            "GROUP BY LOWER(artist) ORDER BY plays DESC, ms DESC, name LIMIT :n", {"n": limit})
    ]
    result["top_tracks"] = [
        {"title": r["title"], "artist": r["artist"], "album": r["album"], "plays": r["plays"], "minutes": round(r["ms"] / 60000), "track_id": r["track_id"]}
        for r in rows(
            "SELECT MIN(title) AS title, MIN(artist) AS artist, MIN(album) AS album, COUNT(*) AS plays, SUM(ms) AS ms, MAX(track_id) AS track_id "
            "FROM p WHERE title IS NOT NULL GROUP BY LOWER(artist), LOWER(title) ORDER BY plays DESC, ms DESC, title LIMIT :n", {"n": limit})
    ]
    result["top_albums"] = [
        {"album": r["album"], "artist": r["artist"], "plays": r["plays"], "track_id": r["track_id"]}
        for r in rows(
            "SELECT MIN(album) AS album, MIN(artist) AS artist, COUNT(*) AS plays, MAX(track_id) AS track_id FROM p WHERE album IS NOT NULL "
            "GROUP BY LOWER(artist), LOWER(album) ORDER BY plays DESC, album LIMIT :n", {"n": limit})
    ]
    result["top_genres"] = [
        {"genre": r["genre"], "plays": r["plays"]}
        for r in rows("SELECT MIN(genre) AS genre, COUNT(*) AS plays FROM p WHERE genre IS NOT NULL GROUP BY LOWER(genre) ORDER BY plays DESC, genre LIMIT :n", {"n": limit})
    ]

    months = {r["month"]: r for r in rows(
        "SELECT strftime('%Y-%m', ts, 'unixepoch') AS month, COUNT(*) AS plays, SUM(ms) AS ms FROM p GROUP BY month")}
    ordered = sorted(months)
    result["by_month"] = [
        {"month": key, "plays": months[key]["plays"] if key in months else 0, "minutes": round(months[key]["ms"] / 60000) if key in months else 0}
        for key in _month_keys(ordered[0], ordered[-1])
    ]
    by_hour = [0] * 24
    for r in rows("SELECT CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) AS hour, COUNT(*) AS plays FROM p GROUP BY hour"):
        by_hour[r["hour"]] = r["plays"]
    by_weekday = [0] * 7
    for r in rows("SELECT CAST(strftime('%w', ts, 'unixepoch') AS INTEGER) AS day, COUNT(*) AS plays FROM p GROUP BY day"):
        by_weekday[(r["day"] + 6) % 7] = r["plays"]  # SQLite counts Sunday as 0; weeks here start on Monday
    result["by_hour"], result["by_weekday"] = by_hour, by_weekday

    days = [(r["day"], r["plays"]) for r in rows("SELECT date(ts, 'unixepoch') AS day, COUNT(*) AS plays FROM p GROUP BY day ORDER BY day")]
    result["by_day"] = [{"date": d, "plays": n} for d, n in days] if (end - start) <= 370 * 86400 else []
    result["streak"] = _longest_streak([d for d, _ in days])

    new = conn.execute(
        PLAYS.replace("WHERE h.played_at_epoch >= :start AND h.played_at_epoch < :end", "")
        + "SELECT MIN(artist) AS name, MIN(at) AS first, COUNT(*) AS plays FROM p WHERE artist IS NOT NULL GROUP BY LOWER(artist) "
          "HAVING MIN(at) >= :start AND MIN(at) < :end ORDER BY plays DESC, name",
        args,
    ).fetchall()
    result["discoveries"] = {"count": len(new), "items": [{"name": r["name"], "plays": r["plays"], "first_play": r["first"]} for r in new[:limit]]}

    result["month_favorites"] = [
        {"month": r["month"], "title": r["title"], "artist": r["artist"], "plays": r["plays"], "track_id": r["track_id"]}
        for r in rows(
            "SELECT month, title, artist, plays, track_id FROM ("
            " SELECT strftime('%Y-%m', ts, 'unixepoch') AS month, MIN(title) AS title, MIN(artist) AS artist, COUNT(*) AS plays, MAX(track_id) AS track_id,"
            "  ROW_NUMBER() OVER (PARTITION BY strftime('%Y-%m', ts, 'unixepoch') ORDER BY COUNT(*) DESC, MIN(title)) AS rank"
            " FROM p WHERE title IS NOT NULL GROUP BY month, LOWER(artist), LOWER(title)) WHERE rank = 1 ORDER BY month")
    ]
    return result
