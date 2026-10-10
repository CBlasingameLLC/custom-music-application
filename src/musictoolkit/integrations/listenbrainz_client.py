from __future__ import annotations

from typing import Any

import requests

_BASE_URL = "https://api.listenbrainz.org/1"
MAX_LISTENS_PER_REQUEST = 1000
TIMEOUT = 30


class ListenBrainzError(Exception):
    """A request ListenBrainz refused, or one that never got there.

    `status` is the HTTP status, or None when there was no answer at all (offline, DNS, timeout).
    `retry_after` is how many seconds the service asked us to wait, when it said."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def token_rejected(self) -> bool:
        return self.status == 401

    @property
    def transient(self) -> bool:
        """Worth trying again later: no connection, too many requests, or a problem on their side."""
        return self.status is None or self.status == 429 or self.status >= 500


def _wait_hint(response: requests.Response) -> float | None:
    for header in ("Retry-After", "X-RateLimit-Reset-In"):
        try:
            return float(response.headers.get(header) or "")
        except ValueError:
            continue
    return None


def _headers(user_token: str) -> dict[str, str]:
    return {"Authorization": f"Token {user_token}", "Content-Type": "application/json"}


def _send(method: str, path: str, user_token: str | None, **kwargs: Any) -> requests.Response:
    headers = _headers(user_token) if user_token else {}
    try:
        return requests.request(method, f"{_BASE_URL}{path}", headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise ListenBrainzError(f"could not reach ListenBrainz ({type(exc).__name__})") from exc


def _fail(what: str, response: requests.Response) -> ListenBrainzError:
    return ListenBrainzError(f"{what} failed ({response.status_code}): {response.text}", response.status_code, _wait_hint(response))


def submit_listens(user_token: str, listens: list[dict], listen_type: str = "import") -> None:
    """Submit listens. Callers are expected to chunk to MAX_LISTENS_PER_REQUEST
    themselves (kept as the caller's responsibility so the DB-side 'which rows
    made it' bookkeeping stays per-batch and resumable). Each listen needs
    'listened_at' (UNIX epoch seconds) and a 'track_metadata' dict with at
    least 'artist_name' and 'track_name'."""
    if len(listens) > MAX_LISTENS_PER_REQUEST:
        raise ValueError(f"submit_listens called with {len(listens)} listens, max is {MAX_LISTENS_PER_REQUEST}")
    response = _send("POST", "/submit-listens", user_token, json={"listen_type": listen_type, "payload": listens})
    if response.status_code != 200:
        raise _fail("submit-listens", response)


def playing_now(user_token: str, track_metadata: dict) -> None:
    """Tell ListenBrainz what is playing right now. It is shown for a few minutes and never kept as a listen."""
    response = _send("POST", "/submit-listens", user_token, json={"listen_type": "playing_now", "payload": [{"track_metadata": track_metadata}]})
    if response.status_code != 200:
        raise _fail("playing-now", response)


def validate_token(user_token: str) -> dict:
    """Ask ListenBrainz whether a token is good. Returns {"valid": bool, "user_name": str | None, "message": str}."""
    response = _send("GET", "/validate-token", user_token)
    if response.status_code == 401:
        return {"valid": False, "user_name": None, "message": "ListenBrainz does not recognise this token."}
    if response.status_code != 200:
        raise _fail("validate-token", response)
    data = response.json()
    return {"valid": bool(data.get("valid")), "user_name": data.get("user_name"), "message": data.get("message") or ""}


def get_cf_recommendations(user_name: str, user_token: str | None, count: int = 50, offset: int = 0) -> list[dict]:
    """Fetch this user's collaborative-filtering recording recommendations —
    raw recording MBIDs + scores generated server-side by ListenBrainz, not
    a from-scratch recommender. Each entry needs a follow-up MusicBrainz
    lookup to resolve into an artist/track name."""
    response = _send("GET", f"/cf/recommendation/user/{user_name}/recording", user_token, params={"count": count, "offset": offset})
    if response.status_code != 200:
        raise _fail("recommendation fetch", response)
    return response.json().get("payload", {}).get("mbids", [])
