"""Sending listens to ListenBrainz without letting one bad listen spoil the rest, or a hiccup spoil the run."""

from __future__ import annotations

import time
from typing import Any, Callable

from musictoolkit.integrations.listenbrainz_client import ListenBrainzError


def submit_with_isolation(
    client: Any,
    token: str,
    rows: list[Any],
    to_payload: Callable[[Any], dict],
    on_sent: Callable[[list[Any]], None],
    on_refused: Callable[[Any], None],
) -> None:
    """Send `rows` as one request. ListenBrainz answers 400 when something in a batch is unacceptable and does not
    say what, so the batch is halved until the offending listens are alone; each of those goes to `on_refused` and
    everything else still goes through. Any other failure (no connection, rejected token, busy service) is raised."""
    try:
        client.submit_listens(token, [to_payload(row) for row in rows])
    except ListenBrainzError as exc:
        if exc.status != 400:
            raise
        if len(rows) == 1:
            on_refused(rows[0])
            return
        middle = len(rows) // 2
        submit_with_isolation(client, token, rows[:middle], to_payload, on_sent, on_refused)
        submit_with_isolation(client, token, rows[middle:], to_payload, on_sent, on_refused)
        return
    on_sent(rows)


def with_patience(call: Callable[[], None], sleep: Callable[[float], None] = time.sleep, attempts: int = 5) -> None:
    """Run `call`, waiting and trying again when ListenBrainz is busy or unreachable (pauses of 5 s doubling to 2 min,
    longer if the service asked for it). A refusal that waiting cannot fix is raised at once."""
    for attempt in range(attempts):
        try:
            call()
            return
        except ListenBrainzError as exc:
            if not exc.transient or attempt == attempts - 1:
                raise
            sleep(min(max(exc.retry_after or 0, 5 * 2**attempt), 120))
