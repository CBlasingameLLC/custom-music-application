from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from musictoolkit.integrations import lastfm_client, listenbrainz_client, musicbrainz_client

logger = logging.getLogger("musictoolkit")


@dataclass
class Candidate:
    artist_name: str
    track_name: str | None
    musicbrainz_artist_id: str | None
    musicbrainz_recording_id: str | None
    source: str
    score: float
    reason: str


def _normalize(value) -> str:
    if not value:
        return ""
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


def fetch_listenbrainz_candidates(user_name: str, user_token: str | None, limit: int) -> list[Candidate]:
    candidates = []
    for entry in listenbrainz_client.get_cf_recommendations(user_name, user_token, count=limit):
        mbid = entry.get("recording_mbid")
        if not mbid:
            continue
        recording = musicbrainz_client.get_recording(mbid)
        if recording is None:
            continue

        artist_id = None
        artist_credit_list = recording.get("artist-credit") or []
        if artist_credit_list and isinstance(artist_credit_list[0], dict):
            artist_id = (artist_credit_list[0].get("artist") or {}).get("id")

        candidates.append(
            Candidate(
                artist_name=recording.get("artist-credit-phrase") or "Unknown Artist",
                track_name=recording.get("title"),
                musicbrainz_artist_id=artist_id,
                musicbrainz_recording_id=mbid,
                source="listenbrainz_cf",
                score=float(entry.get("score", 0.0)),
                reason="ListenBrainz collaborative filtering",
            )
        )
    return candidates


def fetch_lastfm_candidates(seed_artists: list[str], limit_per_artist: int) -> list[Candidate]:
    candidates = []
    for artist in seed_artists:
        for similar in lastfm_client.get_similar_artists(artist, limit=limit_per_artist):
            candidates.append(
                Candidate(
                    artist_name=similar["name"],
                    track_name=None,
                    musicbrainz_artist_id=similar.get("mbid"),
                    musicbrainz_recording_id=None,
                    source="lastfm_similar",
                    score=similar.get("match", 0.0),
                    reason=f"Similar to {artist} (Last.fm)",
                )
            )
    return candidates


def top_played_artists(conn: sqlite3.Connection, limit: int = 10) -> list[str]:
    rows = conn.execute(
        """
        SELECT raw_artist_name, COUNT(*) AS play_count
        FROM play_history
        WHERE raw_artist_name IS NOT NULL
        GROUP BY raw_artist_name
        ORDER BY play_count DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["raw_artist_name"] for row in rows]


def filter_owned(conn: sqlite3.Connection, candidates: list[Candidate]) -> list[Candidate]:
    """The crux of 'genuinely new suggestions': drop anything already owned.
    MBID match first (exact), normalized artist+title fallback. A false
    negative here (an owned artist wrongly surfacing as new) is the single
    most important failure mode to verify."""
    owned_recording_mbids = {
        row["musicbrainz_recording_id"]
        for row in conn.execute(
            "SELECT musicbrainz_recording_id FROM tracks WHERE musicbrainz_recording_id IS NOT NULL"
        )
    }
    owned_artist_mbids = {
        row["musicbrainz_artist_id"]
        for row in conn.execute("SELECT musicbrainz_artist_id FROM tracks WHERE musicbrainz_artist_id IS NOT NULL")
    }
    owned_artist_title_pairs = {
        (_normalize(row["artist"]), _normalize(row["title"]))
        for row in conn.execute("SELECT artist, title FROM tracks WHERE is_missing = 0")
    }
    owned_artist_names = {
        _normalize(row["artist"])
        for row in conn.execute("SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL")
    }

    new_candidates = []
    for c in candidates:
        if c.musicbrainz_recording_id and c.musicbrainz_recording_id in owned_recording_mbids:
            continue
        if c.musicbrainz_artist_id and c.musicbrainz_artist_id in owned_artist_mbids:
            continue
        if c.track_name:
            if (_normalize(c.artist_name), _normalize(c.track_name)) in owned_artist_title_pairs:
                continue
        elif _normalize(c.artist_name) in owned_artist_names:
            # Artist-only candidate (e.g. a Last.fm similar-artist result) — already own this artist.
            continue
        new_candidates.append(c)
    return new_candidates


def compute_play_history_weights(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT raw_artist_name, COUNT(*) AS play_count
        FROM play_history
        WHERE raw_artist_name IS NOT NULL
        GROUP BY raw_artist_name
        """
    ).fetchall()
    if not rows:
        return {}
    max_count = max(row["play_count"] for row in rows)
    return {_normalize(row["raw_artist_name"]): row["play_count"] / max_count for row in rows}


def rank(candidates: list[Candidate], play_history_weight: dict[str, float]) -> list[Candidate]:
    """Simple, transparent weighted ranking — deliberately not a black box.
    Different sources use non-comparable score scales (ListenBrainz's CF
    score vs. Last.fm's 0-1 match), so each source's own score is first
    normalized to 0-1 within this batch, then combined with a bonus for
    artists that already show up in the user's own play history."""
    max_by_source: dict[str, float] = {}
    for c in candidates:
        max_by_source[c.source] = max(max_by_source.get(c.source, 0.0), c.score)

    def combined_score(c: Candidate) -> float:
        denominator = max_by_source[c.source] or 1.0
        normalized = c.score / denominator
        return normalized + play_history_weight.get(_normalize(c.artist_name), 0.0)

    return sorted(candidates, key=combined_score, reverse=True)


def save_recommendations(conn: sqlite3.Connection, candidates: list[Candidate]) -> int:
    """Dedup key is (artist_name, track_name, source) — track_name is
    coalesced to '' for artist-only candidates because SQL UNIQUE treats
    every NULL as distinct, which would otherwise re-insert the same
    artist-only suggestion on every run instead of deduping it."""
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    for c in candidates:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO recommendations (
                artist_name, track_name, musicbrainz_artist_id, musicbrainz_recording_id,
                source, score, reason, status, date_suggested
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                c.artist_name, c.track_name or "", c.musicbrainz_artist_id, c.musicbrainz_recording_id,
                c.source, c.score, c.reason, now,
            ),
        )
        if cursor.rowcount:
            saved += 1
    conn.commit()
    return saved
