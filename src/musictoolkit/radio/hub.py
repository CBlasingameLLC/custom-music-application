"""What is open right now: how many streams, and what each station last said was playing."""

from __future__ import annotations

import threading
import time
from typing import Any

from musictoolkit.net.proxy import Slots
from musictoolkit.radio.icy import split_title

MAX_STREAMS = 4


class RadioHub:
    def __init__(self, max_streams: int = MAX_STREAMS) -> None:
        self.slots = Slots(max_streams)
        self._now: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def heard(self, station_id: int, title: str | None) -> None:
        """The station's stream just said this is playing (None: it said nothing is, or nothing yet)."""
        with self._lock:
            known = self._now.get(station_id)
            if title and (known is None or known["title"] != title):
                artist, track = split_title(title)
                self._now[station_id] = {"title": title, "artist": artist, "track": track, "since": time.time()}
            elif not title:
                self._now.pop(station_id, None)

    def now(self, station_id: int) -> dict[str, Any]:
        with self._lock:
            return dict(self._now.get(station_id) or {"title": None, "artist": None, "track": None, "since": None})

    def forget(self, station_id: int) -> None:
        with self._lock:
            self._now.pop(station_id, None)
