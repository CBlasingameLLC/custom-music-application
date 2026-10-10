from __future__ import annotations

import musicbrainzngs
import requests

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


def get_recording(mbid: str) -> dict | None:
    """Resolve a bare recording MBID (e.g. from a ListenBrainz recommendation)
    to its title and artist-credit. Returns None if the MBID doesn't resolve."""
    if not _configured:
        raise RuntimeError("musicbrainz_client.configure() must be called first")
    try:
        result = musicbrainzngs.get_recording_by_id(mbid, includes=["artists"])
    except musicbrainzngs.ResponseError:
        return None
    return result.get("recording")


def ping(app_name: str, app_version: str, contact: str) -> tuple[bool, str]:
    """Look up one well-known artist, introducing the app the way MusicBrainz asks, to learn whether it is reachable."""
    try:
        response = requests.get(
            "https://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da",
            params={"fmt": "json"},
            headers={"User-Agent": f"{app_name}/{app_version} ( {contact} )"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, f"Could not reach MusicBrainz ({type(exc).__name__})."
    if response.status_code == 200:
        return True, "MusicBrainz is reachable and accepted the request."
    if response.status_code in (429, 503):
        return False, "MusicBrainz is busy right now (it limits how fast apps may ask). Try again in a minute."
    return False, f"MusicBrainz answered with error {response.status_code}."
