from __future__ import annotations

import pylast
import requests

_network: pylast.LastFMNetwork | None = None


def configure(api_key: str, api_secret: str) -> None:
    global _network
    _network = pylast.LastFMNetwork(api_key=api_key, api_secret=api_secret)


def get_similar_artists(artist_name: str, limit: int = 10) -> list[dict]:
    """Secondary/optional recommendation source — ListenBrainz is primary.
    Returns [] on any lookup failure (e.g. artist not found) rather than
    raising, since one bad seed artist shouldn't abort a whole recommend run."""
    if _network is None:
        raise RuntimeError("lastfm_client.configure() must be called first")
    try:
        artist = _network.get_artist(artist_name)
        similar_items = artist.get_similar(limit=limit)
    except pylast.WSError:
        return []

    results = []
    for item in similar_items:
        try:
            mbid = item.item.get_mbid()
        except Exception:
            mbid = None
        results.append({"name": item.item.get_name(), "match": float(item.match), "mbid": mbid or None})
    return results


def check_key(api_key: str) -> tuple[bool, str]:
    """Ask Last.fm for the smallest thing it offers, to learn whether it accepts this key."""
    try:
        response = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={"method": "chart.getTopArtists", "api_key": api_key, "format": "json", "limit": 1},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, f"Could not reach Last.fm ({type(exc).__name__})."
    try:
        data = response.json()
    except ValueError:
        data = {}
    if isinstance(data, dict) and "error" in data:  # e.g. {"error": 10, "message": "Invalid API key ..."}
        return False, f"Last.fm says: {data.get('message') or 'the key was refused'}"
    if response.status_code != 200:
        return False, f"Last.fm answered with error {response.status_code}."
    return True, "Last.fm accepted the key."
