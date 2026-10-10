"""Sends plays from the in-app player to ListenBrainz, in the background.

Every play is written to the local history first (source 'future_scrobble'), so nothing depends on the
network. This thread sends the ones ListenBrainz has not seen yet, in small batches, and only when the person
has saved a ListenBrainz token and left scrobbling on. When the network or the service is down it keeps the
plays and tries again after a growing pause. A rejected token stops it until the token changes (or the person
presses Try again). A play ListenBrainz refuses outright is marked so it can never jam the queue.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from musictoolkit import __version__
from musictoolkit.integrations import listenbrainz_client
from musictoolkit.integrations.listenbrainz_client import ListenBrainzError
from musictoolkit.integrations.submission import submit_with_isolation

logger = logging.getLogger("musictoolkit")

BATCH = 50  # plays per request: small, so one bad play cannot hold up many good ones
IDLE_POLL = 60.0  # seconds between looks at the queue with nothing to do (a new play wakes it sooner)
FIRST_BACKOFF = 30.0
MAX_BACKOFF = 900.0
NOW_PLAYING_EVERY = 10.0  # seconds: skipping through songs should not hammer the service
# play_history.listenbrainz_submitted: 0 = waiting, 1 = sent, 2 = refused by ListenBrainz for good
SENT, REFUSED = 1, 2

_HAS_NAMES = "raw_artist_name IS NOT NULL AND raw_artist_name != '' AND raw_track_name IS NOT NULL AND raw_track_name != ''"
PENDING_WHERE = f"source = 'future_scrobble' AND listenbrainz_submitted = 0 AND {_HAS_NAMES}"


def listen_payload(row: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "artist_name": row["raw_artist_name"],
        "track_name": row["raw_track_name"],
        "additional_info": {"media_player": "Music Toolkit", "submission_client": "Music Toolkit", "submission_client_version": __version__},
    }
    if row["raw_album_name"]:
        meta["release_name"] = row["raw_album_name"]
    return {"listened_at": row["played_at_epoch"], "track_metadata": meta}


def describe_failure(exc: ListenBrainzError) -> str:
    if exc.token_rejected:
        return "ListenBrainz does not accept this token. Copy it again from listenbrainz.org/settings."
    if exc.status is None:
        return "Can't reach ListenBrainz right now. Your plays are kept and sent when it is back."
    if exc.status == 429:
        return "ListenBrainz asked us to slow down. Trying again shortly."
    if exc.status >= 500:
        return f"ListenBrainz is having trouble (error {exc.status}). Trying again shortly."
    return str(exc).splitlines()[0][:200]


class Scrobbler:
    def __init__(self, ctx: Any, client: Any = listenbrainz_client, clock: Any = time.time) -> None:
        self._ctx = ctx
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._rejected_token: str | None = None
        self._last_now_playing = (None, 0.0)
        self._status: dict[str, Any] = {
            "state": "off", "sent": 0, "last_sent_at": None, "last_error": None, "next_attempt_at": None,
            "refused": 0, "last_refused": None,  # plays ListenBrainz would not take, and the latest of them
        }

    # ------------------------------------------------------------------ the thread

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scrobbler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def wake(self) -> None:
        """A new play was recorded (or settings changed): look at the queue now instead of at the next poll."""
        self._wake.set()

    def reset(self) -> None:
        """Forget a rejection and any backoff, e.g. after the token was replaced or the person pressed Try again."""
        self._rejected_token = None
        self._failures = 0
        self.wake()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.run_once()
            except Exception:  # the thread must outlive any one bad moment
                logger.exception("Scrobbler stumbled")
                delay = IDLE_POLL
            if delay > 0:
                self._wake.wait(delay)
            self._wake.clear()

    # ------------------------------------------------------------------ one pass

    def run_once(self) -> float:
        """Send one batch if there is one to send. Returns how many seconds to wait before the next pass (0 = go again)."""
        cfg = self._ctx.config.listenbrainz
        token = cfg.user_token.strip()
        if not (cfg.enabled and cfg.scrobble and token):
            self._set(state="off", next_attempt_at=None)
            return IDLE_POLL
        if self._rejected_token == token:
            return IDLE_POLL  # still 'rejected' until the token changes or the person asks again
        self._rejected_token = None
        try:
            with self._ctx.db() as conn:
                rows = conn.execute(
                    f"SELECT id, played_at_epoch, raw_artist_name, raw_track_name, raw_album_name FROM play_history "
                    f"WHERE {PENDING_WHERE} ORDER BY played_at_epoch, id LIMIT ?",
                    (BATCH,),
                ).fetchall()
                if not rows:
                    self._failures = 0
                    self._set(state="idle", next_attempt_at=None)
                    return IDLE_POLL
                self._set(state="sending")
                self._deliver(conn, token, rows)
        except ListenBrainzError as exc:
            return self._failed(exc, token)
        self._failures = 0
        self._set(state="idle", next_attempt_at=None)
        return 0.0 if len(rows) == BATCH else IDLE_POLL

    def _deliver(self, conn: Any, token: str, rows: list[Any]) -> None:
        submit_with_isolation(
            self._client, token, rows, listen_payload,
            on_sent=lambda sent: self._sent(conn, sent),
            on_refused=lambda row: self._refused(conn, row),
        )

    def _sent(self, conn: Any, rows: list[Any]) -> None:
        self._mark(conn, rows, SENT)
        with self._lock:
            self._status["sent"] += len(rows)
            self._status["last_sent_at"] = self._clock()
            self._status["last_error"] = None

    def _refused(self, conn: Any, row: Any) -> None:
        self._mark(conn, [row], REFUSED)
        with self._lock:
            self._status["refused"] += 1
            self._status["last_refused"] = row["raw_track_name"]

    @staticmethod
    def _mark(conn: Any, rows: list[Any], value: int) -> None:
        conn.executemany("UPDATE play_history SET listenbrainz_submitted = ? WHERE id = ?", [(value, r["id"]) for r in rows])
        conn.commit()

    def _failed(self, exc: ListenBrainzError, token: str) -> float:
        if exc.token_rejected:
            self._rejected_token = token
            self._set(state="rejected", last_error="ListenBrainz does not accept this token. Copy it again from listenbrainz.org/settings.", next_attempt_at=None)
            return IDLE_POLL
        self._failures += 1
        delay = min(MAX_BACKOFF, FIRST_BACKOFF * 2 ** (self._failures - 1))
        if exc.retry_after:
            delay = max(delay, min(exc.retry_after, MAX_BACKOFF))
        self._set(state="waiting", last_error=describe_failure(exc), next_attempt_at=self._clock() + delay)
        return delay

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._status.update(changes)

    # ------------------------------------------------------------------ for the screens

    def pending(self) -> int:
        with self._ctx.db() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM play_history WHERE {PENDING_WHERE}").fetchone()[0]

    def status(self) -> dict[str, Any]:
        cfg = self._ctx.config.listenbrainz
        with self._lock:
            snapshot = dict(self._status)
        active = bool(cfg.enabled and cfg.scrobble and cfg.user_token.strip())
        snapshot.update(active=active, has_token=bool(cfg.user_token.strip()), scrobble=cfg.scrobble, pending=self.pending())
        if not active:
            snapshot["state"] = "off"
        return snapshot

    def now_playing(self, track: dict[str, Any]) -> bool:
        """Best effort: say what is playing. Never raises, and stays quiet unless the person turned it on."""
        cfg = self._ctx.config.listenbrainz
        token = cfg.user_token.strip()
        if not (cfg.enabled and cfg.now_playing and token and track.get("artist") and track.get("title")):
            return False
        if self._rejected_token == token:
            return False
        key, at = self._last_now_playing
        now = self._clock()
        if key == track.get("id") and now - at < NOW_PLAYING_EVERY:
            return False
        self._last_now_playing = (track.get("id"), now)
        meta: dict[str, Any] = {
            "artist_name": track["artist"],
            "track_name": track["title"],
            "additional_info": {"media_player": "Music Toolkit", "submission_client": "Music Toolkit", "submission_client_version": __version__},
        }
        if track.get("album"):
            meta["release_name"] = track["album"]
        try:
            self._client.playing_now(token, meta)
        except ListenBrainzError as exc:
            if exc.token_rejected:
                self._rejected_token = token
                self._set(state="rejected", last_error="ListenBrainz does not accept this token. Copy it again from listenbrainz.org/settings.")
            return False
        return True
