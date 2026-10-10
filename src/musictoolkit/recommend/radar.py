"""The release radar: new and announced releases by artists you play.

ListenBrainz works it out from the listens in the person's ListenBrainz account. When that is not set up, or has
nothing yet, MusicBrainz is asked about the artists played most here (or, with no history, the most common in the
library), one artist per request. Releases the library already has are left out. Nothing is downloaded: each release
carries links to look it up, the way Discover does.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

from musictoolkit.config import Config
from musictoolkit.integrations import listenbrainz_client, musicbrainz_client
from musictoolkit.integrations.listenbrainz_client import ListenBrainzError

PAST_DAYS = 60  # how far back "new" reaches
FUTURE_DAYS = 45  # and how far ahead announced releases are shown
MAX_ARTISTS = 40  # artists asked about one by one on MusicBrainz (about a second each)
MIN_SEEDS_FROM_PLAYS = 10  # fewer artists than this in the history are topped up from the library
WANTED_KINDS = ("Album", "EP", "Single")
MAX_NAME = 300  # characters of an artist or title kept from a service: no real name is longer, and nothing else needs room
NOT_AN_ARTIST = {"", "unknown", "unknown artist", "various", "various artists"}
MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STOP_AFTER_FAILURES = 3  # artists in a row that MusicBrainz would not answer about: it is down or refusing us, so stop
REFRESH_EVERY = timedelta(hours=20)  # "about once a day" without drifting later each day


class RadarUnavailable(RuntimeError):
    """The radar could not look: nowhere to look, or the services did not answer. The message is written for the person."""

    for_the_person = True


@dataclass
class Release:
    mbid: str  # the release group
    artist: str
    artist_mbid: str | None
    title: str
    kind: str  # Album, EP or Single
    date: str  # as the service gave it: YYYY-MM-DD, or YYYY-MM
    source: str  # listenbrainz or musicbrainz
    art_url: str | None = None


def _clip(text: Any) -> str:
    return str(text)[:MAX_NAME]


def _norm(text: Any) -> str:
    return "".join(ch.lower() for ch in str(text or "") if ch.isalnum())


def _day(text: Any) -> date | None:
    """A release date as a day: 'YYYY-MM-DD', or 'YYYY-MM' (its first day). A bare year says too little to call a release new."""
    found = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(text or ""))
    if not found:
        return None
    try:
        return date(int(found[1]), int(found[2]), int(found[3] or 1))
    except ValueError:
        return None


def _kind(primary: Any, secondary: Any) -> str | None:
    """Album, EP or Single, and only those with nothing else about them: a live album, a compilation, a remix or a
    soundtrack is not what "new from an artist you play" means."""
    return primary if primary in WANTED_KINDS and not secondary else None


def window(today: date) -> tuple[date, date]:
    return today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)


# ------------------------------------------------------------ what the services say

def from_listenbrainz(entries: Any, since: date, until: date) -> list[Release]:
    found = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        mbid, title, artist = entry.get("release_group_mbid"), entry.get("release_name"), entry.get("artist_credit_name")
        day = _day(entry.get("release_date"))
        kind = _kind(entry.get("release_group_primary_type"), entry.get("release_group_secondary_type"))
        if not (isinstance(mbid, str) and MBID.fullmatch(mbid) and title and artist and day and kind) or not since <= day <= until:
            continue
        artist_ids = [a for a in entry.get("artist_mbids") or [] if isinstance(a, str) and MBID.fullmatch(a)]
        art_release, art_id = entry.get("caa_release_mbid"), entry.get("caa_id")
        art = None
        if isinstance(art_release, str) and MBID.fullmatch(art_release) and isinstance(art_id, int) and not isinstance(art_id, bool):
            art = f"https://coverartarchive.org/release/{art_release}/{art_id}-250.jpg"
        found.append(Release(mbid, _clip(artist), artist_ids[0] if artist_ids else None, _clip(title), kind, str(entry["release_date"]), "listenbrainz", art))
    return found


def query_for(artist: str, since: date, until: date) -> str:
    quoted = '"' + artist.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"artist:{quoted} AND firstreleasedate:[{since.isoformat()} TO {until.isoformat()}] AND (primarytype:album OR primarytype:ep OR primarytype:single)"


def from_musicbrainz(artist: str, groups: Any, since: date, until: date) -> list[Release]:
    """Only releases credited to this very artist: a search for "Radiohead" also returns "Radiohead 2 and ..."."""
    found = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        mbid, title = group.get("id"), group.get("title")
        day = _day(group.get("first-release-date"))
        kind = _kind(group.get("primary-type"), group.get("secondary-type-list"))
        credit = next(
            (c.get("artist") for c in group.get("artist-credit") or [] if isinstance(c, dict) and isinstance(c.get("artist"), dict) and _norm(c["artist"].get("name")) == _norm(artist)),
            None,
        )
        if not (credit and isinstance(mbid, str) and MBID.fullmatch(mbid) and title and day and kind) or not since <= day <= until:
            continue
        artist_id = credit.get("id") if isinstance(credit.get("id"), str) and MBID.fullmatch(credit["id"]) else None
        found.append(Release(mbid, _clip(credit.get("name") or artist), artist_id, _clip(title), kind, str(group["first-release-date"]), "musicbrainz"))
    return found


# ------------------------------------------------------------ what this library and history say

def seed_artists(conn: sqlite3.Connection, limit: int = MAX_ARTISTS) -> list[str]:
    """The artists to ask MusicBrainz about: the most played here, topped up from the most common in the library."""
    names: list[str] = []
    seen: set[str] = set()

    def add(rows: Iterable[sqlite3.Row]) -> None:
        for row in rows:
            key = _norm(row["name"])
            if key and key not in seen and str(row["name"]).strip().lower() not in NOT_AN_ARTIST and len(names) < limit:
                seen.add(key)
                names.append(str(row["name"]).strip())

    add(conn.execute(
        "SELECT MIN(raw_artist_name) AS name, COUNT(*) AS n FROM play_history WHERE raw_artist_name IS NOT NULL AND TRIM(raw_artist_name) != '' "
        "GROUP BY LOWER(TRIM(raw_artist_name)) ORDER BY n DESC, name LIMIT ?", (limit * 2,)))
    if len(names) < MIN_SEEDS_FROM_PLAYS:
        add(conn.execute(
            "SELECT MIN(artist) AS name, COUNT(*) AS n FROM tracks WHERE is_missing = 0 AND artist IS NOT NULL AND TRIM(artist) != '' "
            "GROUP BY LOWER(TRIM(artist)) ORDER BY n DESC, name LIMIT ?", (limit * 2,)))
    return names


def plays_by_artist(conn: sqlite3.Connection) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in conn.execute("SELECT raw_artist_name AS name, COUNT(*) AS n FROM play_history WHERE raw_artist_name IS NOT NULL GROUP BY LOWER(raw_artist_name)"):
        totals[_norm(row["name"])] = totals.get(_norm(row["name"]), 0) + row["n"]
    return totals


def owned_pairs(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(artist, album or song title) for everything in the library, so a release that is already here is left out."""
    pairs: set[tuple[str, str]] = set()
    for row in conn.execute("SELECT artist, album_artist, album, title FROM tracks WHERE is_missing = 0"):
        for who in (row["artist"], row["album_artist"]):
            for what in (row["album"], row["title"]):
                if who and what:
                    pairs.add((_norm(who), _norm(what)))
    return pairs


# ------------------------------------------------------------ keeping them

def _remember(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (f"radar.{key}", value))


def state(conn: sqlite3.Connection) -> dict[str, Any]:
    kept = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'radar.%'")}
    return {"last_run": kept.get("radar.last_run"), "source": kept.get("radar.source") or None, "message": kept.get("radar.message") or None}


def due(last_run: str | None, now: datetime) -> bool:
    """Has it been long enough since the last look for an automatic one?"""
    if not last_run:
        return True
    try:
        then = datetime.fromisoformat(last_run)
    except ValueError:
        return True
    return now - then >= REFRESH_EVERY


def save(conn: sqlite3.Connection, releases: Iterable[Release], plays: dict[str, int], owned: set[tuple[str, str]], since: date, now: datetime) -> dict[str, int]:
    """Keep what is new to the library, remembering what the person already dismissed; forget what is too old to be news."""
    added = known = owned_already = 0
    for release in releases:
        if (_norm(release.artist), _norm(release.title)) in owned:
            owned_already += 1
            continue
        existing = conn.execute("SELECT 1 FROM fresh_releases WHERE release_group_mbid = ?", (release.mbid,)).fetchone()
        conn.execute(
            "INSERT INTO fresh_releases (release_group_mbid, artist, artist_mbid, title, kind, release_date, source, art_url, plays, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(release_group_mbid) DO UPDATE SET "
            "release_date = excluded.release_date, title = excluded.title, kind = excluded.kind, plays = excluded.plays, "
            "art_url = COALESCE(excluded.art_url, fresh_releases.art_url)",
            (release.mbid, release.artist, release.artist_mbid, release.title, release.kind, release.date, release.source, release.art_url,
             plays.get(_norm(release.artist), 0), now.isoformat()),
        )
        known += 1 if existing else 0
        added += 0 if existing else 1
    conn.execute("DELETE FROM fresh_releases WHERE release_date < ?", (since.isoformat(),))
    conn.commit()
    return {"new": added, "known": known, "owned": owned_already}


# ------------------------------------------------------------ the whole thing

def refresh(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    today: date | None = None,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
    check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Look for new releases and keep them. `check` is called between requests and may raise to stop (a cancelled job).

    Returns {"source", "found", "new", "known", "owned", "artists", "messages"}; raises RadarUnavailable when there
    was nowhere to look."""
    say = progress or (lambda _message: None)
    stop = check or (lambda: None)
    now = now or datetime.now(timezone.utc)
    since, until = window(today or now.date())
    messages: list[str] = []
    releases: list[Release] = []
    source = None
    artists_checked = 0
    answered = False  # a service really answered (even with nothing): otherwise there is nothing to keep, and it will be tried again soon
    contact = cfg.musicbrainz.contact.strip()
    has_listenbrainz = bool(cfg.listenbrainz.enabled and cfg.listenbrainz.username)
    if not has_listenbrainz and not contact:
        raise RadarUnavailable(
            "There is nowhere to look yet. Add your ListenBrainz username (Settings, Accounts), or a MusicBrainz contact: "
            "an email address or a web address, which MusicBrainz asks every app to give."
        )

    if has_listenbrainz:
        say("Asking ListenBrainz for new releases by artists you listen to…")
        try:
            releases = from_listenbrainz(
                listenbrainz_client.get_fresh_releases(cfg.listenbrainz.username, PAST_DAYS, cfg.listenbrainz.user_token or None), since, until)
            source, answered = "listenbrainz", True
            if not releases:
                messages.append("ListenBrainz has no new releases for you yet (it learns from the plays sent to your account).")
        except ListenBrainzError as exc:
            messages.append(f"ListenBrainz: {str(exc).splitlines()[0][:160]}")

    if not releases:
        if not contact:
            messages.append("To look on MusicBrainz as well, add a contact in Settings, Accounts.")
        else:
            artists = seed_artists(conn)
            if not artists:
                messages.append("There is no listening history or music in the library to look from yet.")
            else:
                musicbrainz_client.configure(cfg.musicbrainz.app_name, cfg.musicbrainz.app_version, contact)
                failures = 0
                for number, artist in enumerate(artists, start=1):
                    stop()
                    say(f"Looking up {artist} on MusicBrainz ({number} of {len(artists)})")
                    try:
                        releases.extend(from_musicbrainz(artist, musicbrainz_client.search_release_groups(query_for(artist, since, until)), since, until))
                        failures = 0
                        source, answered = "musicbrainz", True
                    except Exception as exc:  # the network or the service: stop if it keeps happening
                        failures += 1
                        if failures >= STOP_AFTER_FAILURES:
                            messages.append(f"MusicBrainz did not answer ({type(exc).__name__}); stopped after {number} of {len(artists)} artists.")
                            break
                    artists_checked = number

    if not answered:
        raise RadarUnavailable(" ".join(messages))

    stop()
    saved = save(conn, releases, plays_by_artist(conn), owned_pairs(conn), since, now)
    _remember(conn, "last_run", now.isoformat())
    _remember(conn, "source", source or "")
    _remember(conn, "message", " ".join(messages))
    conn.commit()
    return {"source": source, "found": len(releases), "artists": artists_checked, "messages": messages, **saved}


# ------------------------------------------------------------ what the screen shows

def links(artist: str, title: str, mbid: str) -> list[dict[str, str]]:
    """Where to look at it. Getting the music stays a deliberate step: nothing is downloaded."""
    query = quote_plus(f"{artist} {title}".strip())
    return [
        {"label": "MusicBrainz", "url": f"https://musicbrainz.org/release-group/{mbid}"},
        {"label": "Bandcamp", "url": f"https://bandcamp.com/search?q={query}"},
        {"label": "YouTube", "url": f"https://www.youtube.com/results?search_query={query}"},
    ]
