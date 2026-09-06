from __future__ import annotations

import musicbrainzngs

_configured = False


def configure(app_name: str, app_version: str, contact: str) -> None:
    """Must be called once before any lookup — MusicBrainz's API etiquette
    requires a real contact string in the User-Agent so they can reach the
    developer if a client misbehaves."""
    global _configured
    musicbrainzngs.set_useragent(app_name, app_version, contact)
    musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)
    _configured = True


def search_recording(artist: str, title: str, limit: int = 5) -> list[dict]:
    if not _configured:
        raise RuntimeError("musicbrainz_client.configure() must be called first")
    result = musicbrainzngs.search_recordings(artist=artist, recording=title, limit=limit)
    return result.get("recording-list", [])


def best_match(artist: str, title: str) -> dict | None:
    """Return MusicBrainz's single highest-relevance-score candidate, or None."""
    candidates = search_recording(artist, title)
    if not candidates:
        return None
    return max(candidates, key=lambda rec: int(rec.get("ext:score", 0)))
