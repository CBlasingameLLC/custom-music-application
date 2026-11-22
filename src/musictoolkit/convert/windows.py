"""Songs only Windows can decode (M4A, AAC, WMA): asking the disc helper what they are like, and having it decode one to a temporary WAV file.

The same helper commands the music CD burn uses (`inspect`, `decode`); on a computer without the helper or without Windows' decoders every such
song is simply one that cannot be converted, with the reason in words.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from musictoolkit.discs.burnjob import _carrying_stop as carrying_stop  # (the one way a press of Stop reaches the helper while it works)
from musictoolkit.discs.helper import DiscError, DiscHelper, DiscUnavailable

__all__ = ["BATCH", "ask", "carrying_stop", "decode"]

BATCH = 50  # songs asked about at a time


def ask(paths: list[str]) -> dict[str, dict[str, Any]]:
    """What Windows says of each song, as the helper words it: {"ok": True, "codec", "rate", "channels", "seconds"} or {"ok": False, "error"}.
    If the helper cannot be started or asked, each song is answered with the reason."""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    answers: dict[str, dict[str, Any]] = {}
    try:
        with DiscHelper() as helper:
            for first in range(0, len(paths), BATCH):
                for entry in helper.call("inspect", paths=paths[first : first + BATCH]).get("songs") or []:
                    answers[str(entry.get("path"))] = entry
    except (DiscError, DiscUnavailable) as error:
        reason = f"Windows could not be asked to decode this song ({str(error).rstrip('.')})."
        return {p: {"ok": False, "error": reason} for p in paths}
    return {p: answers.get(p) or {"ok": False, "error": "Windows could not say what is in this song."} for p in paths}


def decode(helper: DiscHelper, source: str, target: str, on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Have Windows decode `source` to a WAV file at `target` (a name that is not taken; the helper never replaces a file)."""
    return helper.call("decode", src=source, out=target, **({"on_event": on_event} if on_event else {}))
