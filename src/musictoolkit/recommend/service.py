"""Fetch, filter, rank and store recommendations: one implementation for the GUI and the CLI."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from musictoolkit.config import Config
from musictoolkit.integrations import lastfm_client, musicbrainz_client
from musictoolkit.recommend import engine

SOURCES = ("listenbrainz", "lastfm", "both")


@dataclass
class RefreshResult:
    fetched: int = 0
    already_owned: int = 0
    new: int = 0
    saved: int = 0
    messages: list[str] = field(default_factory=list)
    attempted: bool = False  # at least one source was configured and queried
    top: list[engine.Candidate] = field(default_factory=list)


def refresh(
    conn: sqlite3.Connection,
    cfg: Config,
    source: str = "listenbrainz",
    limit: int = 20,
    progress: Callable[[str], None] | None = None,
) -> RefreshResult:
    say = progress or (lambda _message: None)
    result = RefreshResult()
    candidates: list[engine.Candidate] = []

    if source in ("listenbrainz", "both"):
        if not cfg.listenbrainz.enabled:
            result.messages.append("ListenBrainz is turned off in Settings.")
        elif not cfg.listenbrainz.username:
            result.messages.append("No ListenBrainz username is set. Add it in Settings to get recommendations.")
        else:
            result.attempted = True
            say("Asking ListenBrainz for recommendations (each one is looked up on MusicBrainz, about a second apiece)…")
            musicbrainz_client.configure(cfg.musicbrainz.app_name, cfg.musicbrainz.app_version, cfg.musicbrainz.contact)
            try:
                candidates.extend(
                    engine.fetch_listenbrainz_candidates(
                        cfg.listenbrainz.username, cfg.listenbrainz.user_token or None, limit
                    )
                )
            except Exception as exc:
                result.messages.append(f"ListenBrainz fetch failed: {exc}")

    if source in ("lastfm", "both"):
        if not cfg.lastfm.enabled:
            result.messages.append("Last.fm is turned off in Settings.")
        elif not cfg.lastfm.api_key:
            result.messages.append("No Last.fm API key is set. Add it in Settings.")
        else:
            result.attempted = True
            say("Asking Last.fm for artists similar to the ones you play most…")
            lastfm_client.configure(cfg.lastfm.api_key, cfg.lastfm.api_secret)
            try:
                seeds = engine.top_played_artists(conn, limit=10)
                candidates.extend(engine.fetch_lastfm_candidates(seeds, limit_per_artist=5))
            except Exception as exc:
                result.messages.append(f"Last.fm fetch failed: {exc}")

    new_candidates = engine.filter_owned(conn, candidates)
    ranked = engine.rank(new_candidates, engine.compute_play_history_weights(conn))
    result.fetched = len(candidates)
    result.already_owned = len(candidates) - len(new_candidates)
    result.new = len(new_candidates)
    result.saved = engine.save_recommendations(conn, ranked)
    result.top = ranked[:limit]
    return result
