"""Optical drives (CD, DVD, Blu-ray), reached through a small Windows helper program, mtk-disc.exe.

The helper does the hardware part: listing the drives, reading a CD's table of contents and its audio sectors, ejecting, and writing
discs with Windows' own disc burning service (IMAPI). This module talks to it in JSON lines on its standard input and output, the way `musictoolkit.sync.mtp`
does for phones: one request per line ({"id": 1, "cmd": "toc", "drive": "E:"}) and one reply per line ({"id": 1, "ok": true, ...} or
{"id": 1, "ok": false, "code": "no_media", "error": "..."}). The first line the helper prints is {"event": "ready", "version": 1}.

A command that takes long may print {"event": "progress", "id": 1, "done": 3, "total": 10, "message": "..."} lines before its reply, and
{"cmd": "cancel", "target": 1} asks the helper to stop the command it is running (which then answers with the code "cancelled").

Commands: drives; toc {drive}; read {drive, lba, count} (at most 16 sectors; "data" is base64 of 2352 bytes per sector);
flush {drive, lba, count} (read and throw away, to push earlier data out of the drive's own cache); eject {drive}; close.
Writing: media {drive} (what is in a recorder: its kind, whether it is blank, the room, the speeds it writes at); burn_audio {drive, tracks:
[{path, sectors}], speed?, eject?} (raw CD sound the app converted; progress events count sectors, a stop cancels the write); burn_data {drive,
label, label_iso, files: [{src, dest}], filesystems, speed?, eject?}; build_iso {label, label_iso, files, filesystems, out} (makes an image file,
needs no drive, never replaces a file); erase {drive, full} (cannot be stopped).
Sector numbers (lba) count from the start of the first track, as Windows does; a disc's table of contents in MusicBrainz terms adds 150.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger("musictoolkit")

HELPER_FOLDER = "mtk-disc"
HELPER_NAME = "mtk-disc.exe"
PROTOCOL_VERSION = 1
START_TIMEOUT = 30.0  # seconds for the helper to say it is ready
CALL_TIMEOUT = 90.0  # seconds of silence after which a request is given up on (a drive can take a while to spin a disc up)
LISTING_TIMEOUT = 25.0
LISTING_TTL = 3.0  # seconds a list of drives is reused, so a screen that checks every few seconds does not start a helper each time
SECTOR_BYTES = 2352  # one sector of CD audio: 588 stereo frames of 16 bits
MAX_READ_SECTORS = 16
MAX_FLUSH_SECTORS = 4096
DRIVE_ID = re.compile(r"^[A-Za-z]:$")

# What each refusal code means, for the person. The helper's own words (it knows the Windows error) win when it sends some.
MESSAGES = {
    "no_media": "There is no disc in the drive.",
    "media_changed": "The disc was taken out or changed while it was being read.",
    "not_ready": "The drive is not ready yet. Wait until the disc has stopped spinning up and try again.",
    "read_error": "The drive could not read part of the disc.",
    "not_found": "That drive is not there any more.",
    "not_optical": "That is not a CD or DVD drive.",
    "busy": "The drive is busy with something else.",
    "unsupported": "This drive or this disc cannot do that.",
    "not_blank": "This disc is not blank. Put in a blank disc, or erase this one first if it is rewritable (CD-RW, DVD-RW).",
    "write_protected": "This disc cannot be written to: it is write-protected or already closed. Use another disc.",
    "no_space": "That does not fit on this disc.",
    "underrun": "The drive ran out of data while writing, so the disc is probably spoiled. Try again at a lower speed and leave the computer alone while it burns.",
    "cancelled": "Stopped.",
    "timeout": "The drive stopped answering.",
    "stopped": "The disc helper stopped.",
}


class DiscUnavailable(Exception):
    """This computer cannot reach optical drives right now. The message says why, in words for the person."""

    for_the_person = True


class DiscError(Exception):
    """A drive or the helper refused something. `code` says what kind of refusal (see MESSAGES); the message is fit to show."""

    for_the_person = True

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or MESSAGES.get(code) or "The drive did not accept that.")
        self.code = code


def valid_drive(raw: object) -> str:
    """A drive id as the helper knows it ("E:"), from what the page sent. Anything else - a path, a device name - is refused here,
    before it can reach anything that opens a device."""
    text = str(raw or "").strip()
    if not DRIVE_ID.match(text):
        raise DiscError("not_found", "That is not a drive letter.")
    return text.upper()


def helper_command() -> list[str] | None:
    """How to start the helper, or None if it is not here. MTK_DISC_HELPER overrides it (tests and the packaged-app smoke test point it at
    a stand-in): a JSON list of arguments, or one command line."""
    override = os.environ.get("MTK_DISC_HELPER")
    if override:
        if override.lstrip().startswith("["):
            try:
                return [str(part) for part in json.loads(override)]
            except (ValueError, TypeError):
                pass
        return shlex.split(override, posix=os.name != "nt")
    if sys.platform != "win32":
        return None
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / HELPER_FOLDER / HELPER_NAME)  # installed: next to mtk-backend.exe
    candidates.append(Path(__file__).resolve().parents[3] / "helpers" / "disc" / "out" / HELPER_NAME)  # a build from a source checkout
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    return None


def availability() -> tuple[bool, str | None]:
    if helper_command() is not None:
        return True, None
    if sys.platform != "win32":
        return False, "Reading and writing discs is only supported on Windows."
    return False, "The disc helper is missing from this installation. Install Music Toolkit again to get it back."


class DiscHelper:
    """One running helper process. Use it as a context manager. Requests are answered one at a time; `cancel` may be called from
    another thread while a request is waiting for its answer."""

    def __init__(self, command: list[str] | None = None) -> None:
        self._command = command if command is not None else helper_command()
        self._process: subprocess.Popen | None = None
        self._complaints: threading.Thread | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=40)
        self._lock = threading.Lock()  # one request at a time
        self._write_lock = threading.Lock()  # one writer to the helper's input at a time (a cancel may arrive while a request waits)
        self._next_id = 0
        self._current: int | None = None

    def __enter__(self) -> "DiscHelper":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        if not self._command:
            raise DiscUnavailable(availability()[1] or "The disc helper is not available.")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self._process = subprocess.Popen(
                self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
            )
        except OSError as exc:
            raise DiscUnavailable(f"The disc helper could not be started ({exc.strerror or exc}).") from None
        threading.Thread(target=self._read_answers, args=(self._process.stdout,), daemon=True).start()
        self._complaints = threading.Thread(target=self._read_complaints, args=(self._process.stderr,), daemon=True)
        self._complaints.start()
        try:
            hello = self._receive(START_TIMEOUT)
        except DiscError as exc:
            self.close()
            raise DiscUnavailable(f"The disc helper did not start properly: {exc}") from None
        if hello.get("event") != "ready" or hello.get("version") != PROTOCOL_VERSION:
            self.close()
            raise DiscUnavailable("The disc helper is not the version this app expects. Install Music Toolkit again.")

    def _read_answers(self, stream: Any) -> None:
        try:
            for line in stream:
                self._lines.put(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)  # the helper's output ended: whoever is waiting must not wait forever

    def _read_complaints(self, stream: Any) -> None:
        """Keep the helper's error output flowing (an unread pipe fills up and stalls it), and the tail of it for messages."""
        try:
            for line in stream:
                self._stderr.append(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    def _receive(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self.close()
                raise DiscError("timeout") from None
            if line is None:
                if self._complaints is not None:
                    self._complaints.join(timeout=1.0)  # its last words may still be on their way
                tail = " ".join(self._stderr) or "no details"
                raise DiscError("stopped", f"The disc helper stopped ({tail[:300]})")
            if not line.startswith("{"):
                continue  # stray output is not an answer
            try:
                return json.loads(line)
            except ValueError:
                continue

    def _send(self, request: dict[str, Any]) -> None:
        with self._write_lock:
            if self._process is None or self._process.stdin is None:
                raise DiscError("stopped", "The disc helper is not running.")
            try:
                self._process.stdin.write(json.dumps(request) + "\n")
                self._process.stdin.flush()
            except OSError:
                raise DiscError("stopped") from None

    def call(
        self, cmd: str, *, timeout: float | None = None, on_event: Callable[[dict[str, Any]], None] | None = None, **args: Any
    ) -> dict[str, Any]:
        """Send one request and return the helper's reply. A refusal is raised as DiscError. `timeout` is how long the helper may stay
        silent (progress events count as being heard from); `on_event` gets each progress event of this request."""
        with self._lock:
            self._next_id += 1
            request = {"id": self._next_id, "cmd": cmd, **args}
            self._current = request["id"]
            try:
                self._send(request)
                while True:
                    reply = self._receive(timeout or CALL_TIMEOUT)
                    if reply.get("id") != request["id"]:
                        continue  # an answer to something else (a request given up on earlier) is not ours
                    if reply.get("event") == "progress":
                        if on_event is not None:
                            on_event(reply)
                        continue
                    break
            finally:
                self._current = None
            if not reply.get("ok"):
                raise DiscError(str(reply.get("code") or "failed"), reply.get("error") or None)
            return reply

    def cancel(self) -> None:
        """Ask the helper to stop the request it is working on. Safe to call from another thread; does nothing if none is running."""
        target = self._current
        if target is None:
            return
        try:
            self._send({"cmd": "cancel", "target": target})
        except DiscError:
            pass

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin and process.poll() is None:
                process.stdin.write(json.dumps({"id": 0, "cmd": "close"}) + "\n")
                process.stdin.flush()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


_cache: dict[str, Any] = {"at": 0.0, "value": None}
_listing_lock = threading.Lock()
_in_use = 0  # rips and burns that hold a helper open right now
_in_use_lock = threading.Lock()


@contextlib.contextmanager
def in_use() -> Iterator[None]:
    """While a rip or a burn has the drive open, the screens that ask what is in the drives get the last answer instead of starting a
    second helper that talks to the same drive in the middle of a read."""
    global _in_use
    with _in_use_lock:
        _in_use += 1
    try:
        yield
    finally:
        with _in_use_lock:
            _in_use -= 1


def list_drives(fresh: bool = False) -> dict[str, Any]:
    """What the Discs screen needs: {"available": bool, "reason": str | None, "error": str | None, "drives": [...]}. `available` is False
    when this computer has no helper (the reason says why); `error` is set when the helper is there but looking failed. Never raises.
    The answer is reused for a few seconds."""
    with _listing_lock:
        if _cache["value"] is not None and (_in_use or (not fresh and time.monotonic() - _cache["at"] < LISTING_TTL)):
            return _cache["value"]
        ok, reason = availability()
        if not ok:
            result: dict[str, Any] = {"available": False, "reason": reason, "error": None, "drives": []}
        else:
            try:
                with DiscHelper() as helper:
                    drives = helper.call("drives", timeout=LISTING_TIMEOUT)["drives"]
                result = {"available": True, "reason": None, "error": None, "drives": [_tidy(d) for d in drives if isinstance(d, dict) and d.get("id")]}
            except (DiscUnavailable, DiscError) as exc:
                result = {"available": True, "reason": None, "error": str(exc), "drives": []}
        _cache.update(at=time.monotonic(), value=result)
        return result


def _tidy(drive: dict[str, Any]) -> dict[str, Any]:
    return {**drive, "id": str(drive["id"]).upper()}


def forget_listing() -> None:
    """Drop the remembered list (after a disc was ejected, or between tests)."""
    with _listing_lock:
        _cache.update(at=0.0, value=None)
