"""What the tests of the Subsonic API share: asking it the way a client does, and reading what it answers."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import build_web

NAMESPACE = "{http://subsonic.org/restapi}"
VERSION = "1.16.1"


class Rest:
    """A client of /rest over the test client. `password` is sent as `p` unless `auth` says otherwise ("none", "enc", "token", "key")."""

    def __init__(self, client: TestClient, *, password: str | None = None, auth: str = "plain", user: str = "tester", version: str | None = VERSION) -> None:
        self.client, self.password, self.auth, self.user, self.version = client, password, auth, user, version

    def params(self, **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"c": "tests"}
        if self.version is not None:
            out["v"] = self.version
        if self.auth == "key":
            out["apiKey"] = self.password
        else:
            out["u"] = self.user
            if self.password is not None and self.auth == "plain":
                out["p"] = self.password
            elif self.password is not None and self.auth == "enc":
                out["p"] = "enc:" + self.password.encode().hex()
            elif self.password is not None and self.auth == "token":
                salt = "c19b2d"
                out["s"], out["t"] = salt, hashlib.md5((self.password + salt).encode()).hexdigest()  # noqa: S324 (the protocol's own)
        out.update({name: value for name, value in extra.items() if value is not None})
        return out

    def get(self, name: str, /, **extra: Any):
        return self.client.get(f"/rest/{name}", params=self.params(**extra))

    def call(self, name: str, /, **extra: Any) -> dict[str, Any]:
        """The answer in JSON: what is inside `subsonic-response`."""
        response = self.get(name, f="json", **extra)
        assert response.status_code == 200, response.text
        return json.loads(response.text)["subsonic-response"]

    def ok(self, name: str, /, **extra: Any) -> dict[str, Any]:
        answer = self.call(name, **extra)
        assert answer["status"] == "ok", answer
        return answer

    def fails(self, name: str, code: int, /, **extra: Any) -> str:
        """The call fails with this number; the words that come with it."""
        answer = self.call(name, **extra)
        assert answer["status"] == "failed" and answer["error"]["code"] == code, answer
        return answer["error"]["message"]

    def xml(self, name: str, /, **extra: Any) -> ElementTree.Element:
        response = self.get(name, **extra)
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/xml"), response.text
        return ElementTree.fromstring(response.content)


def scanned_web(tmp_path: Path, music_dir: Path | None, token: str | None = None, **options: Any) -> SimpleNamespace:
    """The app over a library that has been scanned (with the token, if the app has one: the scan is asked for like anything else of /api)."""
    web = build_web(tmp_path, music_dir, token=token, **options)
    response = web.client.post("/api/library/scan", headers={"x-mtk-token": token} if token else {})
    assert response.status_code == 200, response.text
    job = web.ctx.jobs.wait(response.json()["job"]["id"], timeout=60)
    assert job.status == "done", job.error
    return web


@contextmanager
def live(app: FastAPI) -> Iterator[int]:
    """The app on a real port of this computer, for a client library that makes its own connections; the port."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        assert time.time() < deadline, "the test server did not start"
        time.sleep(0.02)
    try:
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def add_songs(web: SimpleNamespace, rows: list[tuple[str, str, str, str, int]]) -> None:
    """Songs written straight into the library, for a library bigger than files would make: (file, title, artist, album, track). They are playable, and have no file."""
    with web.ctx.db() as conn:
        conn.executemany(
            "INSERT INTO tracks (file_path, title, artist, album_artist, album, track_number, duration_seconds, date_added) VALUES (?, ?, ?, ?, ?, ?, 60.0, '2024-01-01T00:00:00+00:00')",
            [(file, title, artist, artist, album, track) for file, title, artist, album, track in rows],
        )
        conn.commit()


def songs_of(rest: Rest, album: str) -> list[dict[str, Any]]:
    return rest.ok("getAlbum", id=album)["album"]["song"]


def album_named(rest: Rest, name: str) -> dict[str, Any]:
    found = rest.ok("getAlbumList2", type="alphabeticalByName", size=100)["albumList2"]["album"]
    return next(album for album in found if album["name"] == name)


def tag(element: ElementTree.Element) -> str:
    return element.tag.removeprefix(NAMESPACE)


def _text(value: Any) -> str:
    return "true" if value is True else "false" if value is False else str(value)


def canonical_xml(element: ElementTree.Element) -> dict[str, Any]:
    """An XML answer as plain data: its attributes as text, its child elements as lists (named by tag), its text as `value`."""
    out: dict[str, Any] = dict(element.attrib)
    for child in element:
        out.setdefault(tag(child), []).append(canonical_xml(child))
    if element.text and element.text.strip():
        out["value"] = element.text
    return out


def canonical_json(value: dict[str, Any]) -> dict[str, Any]:
    """The JSON answer as the same plain data (so that what the two forms say can be compared without knowing the protocol): values as text, whatever is a
    dict or a list as a list of dicts (a list of plain values as dicts with only a `value`), and a list with nothing in it, or a text with nothing in it, left out (XML cannot say it)."""
    out: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            out[key] = [canonical_json(item)]
        elif isinstance(item, list):
            if item:
                out[key] = [canonical_json(each) if isinstance(each, dict) else {"value": _text(each)} for each in item]
        elif not (key == "value" and item == ""):  # (text with nothing in it is no text in XML)
            out[key] = _text(item)
    return out
