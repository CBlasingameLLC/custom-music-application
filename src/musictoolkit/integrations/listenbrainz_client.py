from __future__ import annotations

import requests

_BASE_URL = "https://api.listenbrainz.org/1"
MAX_LISTENS_PER_REQUEST = 1000


class ListenBrainzError(Exception):
    pass


def submit_listens(user_token: str, listens: list[dict]) -> None:
    """Submit historical listens. Callers are expected to chunk to
    MAX_LISTENS_PER_REQUEST themselves (kept as the caller's responsibility
    so the DB-side 'which rows made it' bookkeeping stays per-batch and
    resumable). Each listen needs 'listened_at' (UNIX epoch seconds) and a
    'track_metadata' dict with at least 'artist_name' and 'track_name'."""
    if len(listens) > MAX_LISTENS_PER_REQUEST:
        raise ValueError(f"submit_listens called with {len(listens)} listens, max is {MAX_LISTENS_PER_REQUEST}")

    headers = {"Authorization": f"Token {user_token}", "Content-Type": "application/json"}
    payload = {"listen_type": "import", "payload": listens}
    response = requests.post(f"{_BASE_URL}/submit-listens", json=payload, headers=headers, timeout=30)
    if response.status_code != 200:
        raise ListenBrainzError(f"submit-listens failed ({response.status_code}): {response.text}")
