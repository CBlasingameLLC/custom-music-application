"""Phones and players that have no drive letter (MTP).

Windows reaches them through the Windows Portable Devices API. A small helper program, mtk-mtp.exe, does that and
talks to this module in JSON lines on its standard input and output: one request per line
({"id": 1, "cmd": "stat", "storage": "s1", "path": "Music/a.mp3"}) and one reply per line
({"id": 1, "ok": true, ...} or {"id": 1, "ok": false, "code": "no_space", "error": "..."}). The first line the helper
prints is {"event": "ready", "version": 1}. `MtpTarget` lets the sync engine use such a device like any folder.

Commands: devices, open {device}, free {storage}, stat {storage, path}, list {storage, path},
mkdir {storage, path}, delete {storage, path} (a folder goes with everything in it), put {storage, path, source}
(creates the folders and replaces a file that is already there), close.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from musictoolkit.ingest import moves
from musictoolkit.sync.targets import TargetError, join, native, parent_of

logger = logging.getLogger("musictoolkit")

HELPER_FOLDER = "mtk-mtp"
HELPER_NAME = "mtk-mtp.exe"
PROTOCOL_VERSION = 1
START_TIMEOUT = 30.0  # seconds for the helper to say it is ready
CALL_TIMEOUT = 300.0  # seconds for one request (a long song over a slow USB port); a helper that is silent longer is stopped
CODES = {"no_space": errno.ENOSPC, "disconnected": errno.ENODEV, "not_found": errno.ENOENT}
LISTING_TTL = 3.0  # seconds a list of plugged-in devices is reused, so a busy screen does not start a helper per request
DEFAULT_BASE = "Music"
UNKNOWN_FREE = 2**62  # a device that does not say how much room it has is not held back; it refuses when it is full
NOT_CONNECTED = "The phone or player is not connected. Plug it in, unlock it, and choose File transfer on the device."
NO_STORAGE = "The device is connected but its storage is not available. Unlock it and choose File transfer."


class MtpUnavailable(Exception):
    """This computer cannot talk to MTP devices right now. The message says why, in words for the person."""


def helper_command() -> list[str] | None:
    """How to start the helper, or None if it is not here. MTK_MTP_HELPER overrides it (tests and the packaged-app
    smoke test point it at a stand-in): a JSON list of arguments, or one command line."""
    override = os.environ.get("MTK_MTP_HELPER")
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
    candidates.append(Path(__file__).resolve().parents[3] / "helpers" / "mtp" / "out" / HELPER_NAME)  # a build from a source checkout
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    return None


def availability() -> tuple[bool, str | None]:
    if helper_command() is not None:
        return True, None
    if sys.platform != "win32":
        return False, "Phones and players without a drive letter (MTP) are only supported on Windows."
    return False, "The MTP helper is missing from this installation. Install Music Toolkit again to get it back."


class MtpHelper:
    """One running helper process. Use it as a context manager; requests are answered one at a time."""

    def __init__(self, command: list[str] | None = None) -> None:
        self._command = command if command is not None else helper_command()
        self._process: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=40)
        self._lock = threading.Lock()
        self._next_id = 0

    def __enter__(self) -> "MtpHelper":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        if not self._command:
            raise MtpUnavailable(availability()[1] or "The MTP helper is not available.")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self._process = subprocess.Popen(
                self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
            )
        except OSError as exc:
            raise MtpUnavailable(f"The MTP helper could not be started ({exc.strerror or exc}).") from None
        threading.Thread(target=self._read_answers, args=(self._process.stdout,), daemon=True).start()
        threading.Thread(target=self._read_complaints, args=(self._process.stderr,), daemon=True).start()
        try:
            hello = self._receive(START_TIMEOUT)
        except TargetError as exc:
            self.close()
            raise MtpUnavailable(f"The MTP helper did not start properly: {exc}") from None
        if hello.get("event") != "ready" or hello.get("version") != PROTOCOL_VERSION:
            self.close()
            raise MtpUnavailable("The MTP helper is not the version this app expects. Install Music Toolkit again.")

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
                raise TargetError(errno.ETIMEDOUT, "The device stopped answering") from None
            if line is None:
                tail = " ".join(self._stderr) or "no details"
                raise TargetError(errno.ENODEV, f"The MTP helper stopped ({tail[:300]})")
            if not line.startswith("{"):
                continue  # stray output is not an answer
            try:
                return json.loads(line)
            except ValueError:
                continue

    def call(self, cmd: str, *, timeout: float | None = None, **args: Any) -> dict[str, Any]:
        """Send one request and return the helper's reply. A refusal is raised as TargetError (an OSError)."""
        with self._lock:
            if self._process is None or self._process.stdin is None:
                raise TargetError(errno.ENODEV, "The MTP helper is not running")
            self._next_id += 1
            request = {"id": self._next_id, "cmd": cmd, **args}
            try:
                self._process.stdin.write(json.dumps(request) + "\n")
                self._process.stdin.flush()
            except OSError:
                raise TargetError(errno.ENODEV, "The MTP helper stopped") from None
            while True:
                reply = self._receive(timeout or CALL_TIMEOUT)
                if reply.get("id") == request["id"]:
                    break
            if not reply.get("ok"):
                raise TargetError(CODES.get(reply.get("code"), errno.EIO), reply.get("error") or "The device did not accept that")
            return reply

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


LISTING_TIMEOUT = 25.0  # a phone that is asleep or waiting to be unlocked can keep the helper busy for a while

_cache: dict[str, Any] = {"at": 0.0, "value": None}
_listing_lock = threading.Lock()


def list_devices(fresh: bool = False) -> dict[str, Any]:
    """What the Add a device dialog and the device cards need: {"available": bool, "reason": str | None,
    "error": str | None, "devices": [...]}. `available` is False when this computer has no helper (the reason says why);
    `error` is set when the helper is there but looking failed. Never raises. Reused for a few seconds."""
    with _listing_lock:
        if not fresh and _cache["value"] is not None and time.monotonic() - _cache["at"] < LISTING_TTL:
            return _cache["value"]
        ok, reason = availability()
        if not ok:
            result: dict[str, Any] = {"available": False, "reason": reason, "error": None, "devices": []}
        else:
            try:
                with MtpHelper() as helper:
                    result = {"available": True, "reason": None, "error": None, "devices": helper.call("devices", timeout=LISTING_TIMEOUT)["devices"]}
            except (MtpUnavailable, TargetError) as exc:
                result = {"available": True, "reason": None, "error": exc.strerror if isinstance(exc, TargetError) and exc.strerror else str(exc), "devices": []}
        _cache.update(at=time.monotonic(), value=result)
        return result


def forget_listing() -> None:
    """Drop the remembered list (after something changed on a device, or between tests)."""
    with _listing_lock:
        _cache.update(at=0.0, value=None)


def find(devices: list[dict[str, Any]], serial: str, storage: str | None = None, storage_name: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The device with this serial number among those plugged in, and its storage: by the id it had when it was
    added, else by its name (an id can change between plugs, "Internal storage" does not)."""
    device = next((d for d in devices if serial and serial in (d.get("serial"), d.get("id"))), None)
    if device is None:
        return None, None
    storages = device.get("storages", [])
    chosen = next((s for s in storages if storage and s.get("id") == storage), None)
    if chosen is None:
        chosen = next((s for s in storages if any(name and s.get("name") == name for name in (storage_name, storage))), None)
    return device, chosen


class MtpTarget:
    """A device without a drive letter, reached through a running helper. Everything lives under `base` on one storage."""

    kind = "mtp"
    atomic = False  # MTP has no rename: a file is written to its final name, and a failed one is removed again
    case_sensitive = False  # treat names that differ only in case as the same, as FAT-formatted players do

    def __init__(self, helper: MtpHelper, device: dict[str, Any], storage: dict[str, Any], base: str, label: str | None = None) -> None:
        self.helper = helper
        self.device = device
        self.storage = storage
        self.storage_id = storage["id"]
        self.base = native(base).strip("/") or DEFAULT_BASE
        self.label = label or device.get("name") or "Device"

    @classmethod
    def connect(
        cls, helper: MtpHelper, *, serial: str, storage: str, base: str, label: str | None = None, storage_name: str | None = None
    ) -> "MtpTarget":
        """Find the device among those plugged in (by serial number), open it, and check that its storage is there."""
        found, store = find(helper.call("devices", timeout=LISTING_TIMEOUT)["devices"], serial, storage, storage_name)
        if found is None:
            raise TargetError(errno.ENODEV, NOT_CONNECTED)
        if store is None:
            raise TargetError(errno.ENODEV, NO_STORAGE)
        helper.call("open", device=found["id"])
        return cls(helper, found, store, base, label)

    def _path(self, relative: str) -> str:
        return join(self.base, native(relative).strip("/"))

    def _call(self, cmd: str, relative: str | None = None, **args: Any) -> dict[str, Any]:
        if relative is not None:
            args["path"] = self._path(relative)
        return self.helper.call(cmd, storage=self.storage_id, **args)

    def is_connected(self) -> bool:
        try:
            self.helper.call("free", storage=self.storage_id)
            return True
        except (TargetError, MtpUnavailable):
            return False

    def free_space(self) -> int:
        free = self.helper.call("free", storage=self.storage_id).get("free")
        return int(free) if free is not None else UNKNOWN_FREE

    def capacity(self) -> int | None:
        total = self.storage.get("capacity")
        return int(total) if total else None

    def size_of(self, relative: str) -> int | None:
        reply = self._call("stat", relative)
        if not reply.get("exists") or reply.get("is_dir"):
            return None
        return int(reply["size"]) if reply.get("size") is not None else None

    def put(self, source: Path, relative: str) -> int:
        try:
            return int(self._call("put", relative, source=str(source))["size"])
        except TargetError:
            try:
                self._call("delete", relative)  # never leave half a song behind where a player will find it
            except (TargetError, MtpUnavailable):
                pass
            raise

    def delete(self, relative: str) -> None:
        self._call("delete", relative)
        folder = parent_of(relative)
        while folder:  # tidy: a folder holding nothing but a cover picture goes with its last song
            names = self.list_names(folder)
            if any(not (moves.is_folder_art(name) or moves.is_junk(name)) for name in names):
                break
            self._call("delete", folder)
            folder = parent_of(folder)

    def list_names(self, relative_dir: str) -> list[str]:
        try:
            return [entry["name"] for entry in self._call("list", relative_dir)["entries"]]
        except TargetError as exc:
            if exc.errno == errno.ENOENT:
                return []
            raise

    def write_text(self, relative: str, text: str) -> None:
        with tempfile.TemporaryDirectory(prefix="mtk-mtp-") as scratch:
            staging = Path(scratch) / "upload"
            staging.write_text(text, encoding="utf-8")
            self.put(staging, relative)

    def path_problem(self, relative: str) -> str | None:
        return None  # each name is already cut to 255 characters; a device that cannot take more says so when copying
