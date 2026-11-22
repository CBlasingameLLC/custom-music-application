"""Asking Windows about the songs only it reads, and having it decode one: how many it is asked at a time, and what is said when it cannot answer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self

import pytest

from musictoolkit.convert import windows
from musictoolkit.discs.helper import DiscError, DiscUnavailable

SOUND = {"ok": True, "codec": "AAC", "rate": 44100, "channels": 2, "seconds": 200.0}
UNSAID = {"ok": False, "error": "Windows could not say what is in this song."}


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A stand-in for the disc helper that remembers how often it was started and what it was asked, and answers as `reply` says."""
    log = SimpleNamespace(started=0, calls=[], reply=lambda cmd, args: {"songs": [{"path": p, **SOUND} for p in args.get("paths", [])]}, start_error=None, call_error=None)

    class Fake:
        def __enter__(self) -> Self:
            log.started += 1
            if log.start_error:
                raise log.start_error
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def call(self, cmd: str, **args: Any) -> dict[str, Any]:
            log.calls.append((cmd, args))
            if log.call_error:
                raise log.call_error
            return log.reply(cmd, args)

    monkeypatch.setattr(windows, "DiscHelper", Fake)
    return log


def test_nothing_is_asked_about_no_songs_and_the_helper_is_not_even_started(helper: SimpleNamespace) -> None:
    assert windows.ask([]) == {}
    assert helper.started == 0 and helper.calls == []


def test_the_songs_are_asked_in_batches_by_one_helper_and_each_song_once(helper: SimpleNamespace) -> None:
    paths = [f"/music/{n:03d}.m4a" for n in range(windows.BATCH * 2 + 20)]
    answers = windows.ask(paths + paths[:3])  # (three of them named twice)
    assert helper.started == 1  # (one helper for the whole list: starting it is the slow part)
    assert [(cmd, len(args["paths"])) for cmd, args in helper.calls] == [("inspect", windows.BATCH), ("inspect", windows.BATCH), ("inspect", 20)]
    assert [p for _, args in helper.calls for p in args["paths"]] == paths  # (in order, none twice)
    assert list(answers) == paths and all(answers[p] == {"path": p, **SOUND} for p in paths)


def test_a_song_the_answer_leaves_out_is_said_to_be_unknown(helper: SimpleNamespace) -> None:
    helper.reply = lambda cmd, args: {"songs": [{"path": "/music/a.m4a", **SOUND}, {"path": "/music/other.m4a", **SOUND}]}
    answers = windows.ask(["/music/a.m4a", "/music/b.m4a"])
    assert answers == {"/music/a.m4a": {"path": "/music/a.m4a", **SOUND}, "/music/b.m4a": UNSAID}


@pytest.mark.parametrize("reply", [{}, {"songs": None}, {"songs": []}])
def test_an_answer_with_no_songs_in_it_says_nothing_of_any_song(helper: SimpleNamespace, reply: dict[str, Any]) -> None:
    helper.reply = lambda cmd, args: reply
    assert windows.ask(["/music/a.m4a", "/music/b.m4a"]) == {"/music/a.m4a": UNSAID, "/music/b.m4a": UNSAID}


@pytest.mark.parametrize("error,said", [
    (DiscUnavailable("The disc helper is only in the Windows app."), "Windows could not be asked to decode this song (The disc helper is only in the Windows app)."),
    (DiscError("timeout"), "Windows could not be asked to decode this song (The drive stopped answering)."),
])
@pytest.mark.parametrize("when", ["starting", "asking"])
def test_a_helper_that_cannot_be_started_or_asked_is_said_for_every_song(helper: SimpleNamespace, error: Exception, said: str, when: str) -> None:
    if when == "starting":
        helper.start_error = error
    else:
        helper.call_error = error
    assert windows.ask(["/music/a.m4a", "/music/b.m4a"]) == {p: {"ok": False, "error": said} for p in ("/music/a.m4a", "/music/b.m4a")}  # (one full stop, not two)


def test_the_reason_is_given_for_songs_asked_twice_too(helper: SimpleNamespace) -> None:
    helper.start_error = DiscUnavailable("No helper.")
    answers = windows.ask(["/music/a.m4a", "/music/a.m4a"])
    assert list(answers) == ["/music/a.m4a"] and answers["/music/a.m4a"]["error"] == "Windows could not be asked to decode this song (No helper)."


def test_decoding_asks_the_helper_for_the_file_and_passes_progress_on_only_when_it_is_wanted(helper: SimpleNamespace) -> None:
    helper.reply = lambda cmd, args: {"sectors": 5}
    me = windows.DiscHelper()
    assert windows.decode(me, "/music/a.m4a", "/work/a.wav") == {"sectors": 5}
    assert helper.calls[-1] == ("decode", {"src": "/music/a.m4a", "out": "/work/a.wav"})  # (no `on_event` at all: not even an empty one)
    seen: list[dict[str, Any]] = []
    windows.decode(me, "/music/b.m4a", "/work/b.wav", seen.append)
    cmd, args = helper.calls[-1]
    assert (cmd, args["src"], args["out"]) == ("decode", "/music/b.m4a", "/work/b.wav") and args["on_event"] == seen.append
