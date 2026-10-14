"""A fake server for radio and podcast tests: whatever the test scripts, sent over a real local socket.

    def script(h):                      # h is the http.server request handler: h.path, h.headers, h.send_response(), h.wfile ...
        h.send_response(200)
        h.send_header("Content-Type", "audio/mpeg")
        h.end_headers()
        h.wfile.write(b"...")

    with serve_script(script) as server:
        fetch(server.url + "/stream")           # http://127.0.0.1:<port>
        server.requests                          # [{"path": "/stream", "headers": {...}, "at": time.monotonic() when it came}]

Answers are HTTP/1.0 (the body ends when the connection closes), which is how most internet radio servers behave. A client that goes away
mid-stream makes the script's write fail; scripts that loop should stop on OSError.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from xml.sax.saxutils import escape


@contextmanager
def serve_script(script: Callable[[BaseHTTPRequestHandler], None], host: str = "127.0.0.1") -> Iterator[SimpleNamespace]:
    log: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (the name http.server looks for)
            log.append({"path": self.path, "headers": dict(self.headers), "at": time.monotonic()})
            try:
                script(self)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the client went away

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer((host, 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://{host}:{server.server_address[1]}", port=server.server_address[1], requests=log)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def icy_body(audio: bytes, metaint: int, blocks: list[bytes]) -> bytes:
    """What an internet radio server sends when asked for metadata: `metaint` bytes of audio, a metadata block, `metaint` more, ..."""
    body = bytearray()
    for n, block in enumerate(blocks):
        body += audio[n * metaint : (n + 1) * metaint]
        padded = block + b"\0" * (-len(block) % 16)
        body += bytes([len(padded) // 16]) + padded
    return bytes(body)


def radio_script(audio: bytes, *, metaint: int = 1000, titles: list[str] | None = None, content_type: str = "audio/mpeg", name: str = "Test FM", genre: str = "jazz, smooth"):
    """A script for serve_script: answers like an Icecast server (metadata only for a client that asks for it), then closes."""
    titles = titles if titles is not None else ["Miles Davis - So What", "Nina Simone - Feeling Good"]

    def script(h: BaseHTTPRequestHandler) -> None:
        wants = h.headers.get("Icy-MetaData") == "1"
        h.send_response(200)
        h.send_header("Content-Type", content_type)
        h.send_header("icy-name", name)
        h.send_header("icy-genre", genre)
        h.send_header("icy-br", "128")
        if wants:
            h.send_header("icy-metaint", str(metaint))
        h.end_headers()
        if wants:
            h.wfile.write(icy_body(audio, metaint, [f"StreamTitle='{t}';".encode() for t in titles]))
        else:
            h.wfile.write(audio[: metaint * max(1, len(titles))])

    return script


def mp3_frames(data: bytes) -> bytes:
    """The audio of an MP3 file without its ID3v2 tag, so that it can be repeated end to end like a live stream."""
    if data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return data[10 + size :]
    return data


def live_script(
    audio: bytes, *, metaint: int = 4096, titles: list[str] | None = None, change_every: float = 3600.0, name: str = "Live FM", genre: str = "jazz, smooth",
    pause: float = 0.04, blocks: int | None = None,
):
    """A script for serve_script: a station that never ends (unless `blocks` is given: then it hangs up after that many blocks of `metaint` bytes,
    which is a fixed amount of sound whatever the speed of the computer). It repeats `audio` (see mp3_frames) a good deal faster than real time
    and, for a client that asks for it, names the title that is on (a new one every `change_every` seconds) after every block."""
    titles = titles if titles is not None else ["Miles Davis - So What", "Nina Simone - Feeling Good"]

    def script(h: BaseHTTPRequestHandler) -> None:
        wants = h.headers.get("Icy-MetaData") == "1"
        h.send_response(200)
        h.send_header("Content-Type", "audio/mpeg")
        h.send_header("icy-name", name)
        h.send_header("icy-genre", genre)
        h.send_header("icy-br", "48")
        if wants:
            h.send_header("icy-metaint", str(metaint))
        h.end_headers()
        started = time.monotonic()
        position = 0
        sent = 0
        while blocks is None or sent < blocks:
            sent += 1
            block = bytearray()
            while len(block) < metaint:
                piece = audio[position : position + metaint - len(block)]
                block += piece
                position = (position + len(piece)) % len(audio)
            h.wfile.write(bytes(block))
            if wants:
                title = titles[int((time.monotonic() - started) // change_every) % len(titles)] if titles else ""
                meta = f"StreamTitle='{title}';".encode() if title else b""
                padded = meta + b"\0" * (-len(meta) % 16)
                h.wfile.write(bytes([len(padded) // 16]) + padded)
            h.wfile.flush()
            time.sleep(pause)

    return script


def json_script(routes: dict[str, object]):
    """A script for serve_script that answers GET paths (without the query) with JSON: a directory such as Radio Browser or Apple's. A route's
    answer may be a function of the query (a dict of the first value of each parameter) for a directory that answers what it is asked."""
    import json
    from urllib.parse import parse_qs, urlsplit

    def script(h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        path = parts.path
        if path not in routes:
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        answer = routes[path]
        if callable(answer):
            answer = answer({key: values[0] for key, values in parse_qs(parts.query).items()})
        body = json.dumps(answer).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    return script


def png_bytes(size: tuple[int, int] = (600, 400), colour: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def send_audio(h: BaseHTTPRequestHandler, data: bytes, *, ranges: bool = True, content_type: str = "audio/mpeg") -> None:
    """Answer like a podcast host: the whole file, or the part a Range request asks for (when `ranges`; a host that ignores Range sends the whole file)."""
    import re

    asked = re.match(r"bytes=(\d*)-(\d*)$", h.headers.get("Range") or "")
    if asked and ranges and (asked.group(1) or asked.group(2)):
        if asked.group(1):
            start = int(asked.group(1))
            end = min(int(asked.group(2) or len(data) - 1), len(data) - 1)
        else:  # "the last N bytes"
            start, end = max(len(data) - int(asked.group(2)), 0), len(data) - 1
        if start >= len(data):
            h.send_response(416)
            h.send_header("Content-Range", f"bytes */{len(data)}")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        h.send_response(206)
        h.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        h.send_header("Content-Length", str(end - start + 1))
        h.send_header("Content-Type", content_type)
        h.send_header("Accept-Ranges", "bytes")
        h.end_headers()
        h.wfile.write(data[start : end + 1])
        return
    h.send_response(200)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Content-Type", content_type)
    if ranges:
        h.send_header("Accept-Ranges", "bytes")
    h.end_headers()
    h.wfile.write(data)


class PodcastSite:
    """A fake podcast host: /feed.xml (with an ETag, so a second ask can be answered "not modified"), /ep1.mp3 (honours Range), /ep2.mp3 (ignores
    Range), /page.html (not audio) and /art.png. Change `episodes`/`etag` between asks to make the feed change."""

    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.title = "The Test Show"
        self.etag = '"v1"'
        self.feed_status = 200
        self.base = ""
        self.art = png_bytes()
        self.episodes: list[dict] = [
            {"guid": "ep-2", "title": "Second", "path": "/ep2.mp3", "date": "Tue, 03 Jun 2025 10:00:00 +0000", "duration": "0:10:00"},
            {"guid": "ep-1", "title": "First", "path": "/ep1.mp3", "date": "Tue, 27 May 2025 10:00:00 +0000", "duration": "3600"},
        ]

    def feed(self) -> bytes:
        items = "".join(
            f"<item><title>{e['title']}</title><guid>{e['guid']}</guid><pubDate>{e['date']}</pubDate><itunes:duration>{e['duration']}</itunes:duration>"
            + (f"<description>{escape(e['description'])}</description>" if e.get("description") else "")
            + f"<enclosure url=\"{self.base}{e['path']}\" type=\"audio/mpeg\" length=\"{len(self.audio)}\"/></item>"
            for e in self.episodes
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
            f"<title>{self.title}</title><link>https://show.example/</link><description>About the show</description><itunes:author>Jo Host</itunes:author>"
            f'<itunes:image href="{self.base}/art.png"/>{items}</channel></rss>'
        ).encode()

    def script(self):
        def respond(h: BaseHTTPRequestHandler) -> None:
            path = h.path.split("?")[0]
            if path == "/feed.xml":
                if self.feed_status != 200:
                    h.send_response(self.feed_status)
                    h.send_header("Content-Length", "0")
                    h.end_headers()
                elif h.headers.get("If-None-Match") == self.etag:
                    h.send_response(304)
                    h.send_header("ETag", self.etag)
                    h.end_headers()
                else:
                    body = self.feed()
                    h.send_response(200)
                    h.send_header("Content-Type", "application/rss+xml")
                    h.send_header("ETag", self.etag)
                    h.send_header("Content-Length", str(len(body)))
                    h.end_headers()
                    h.wfile.write(body)
            elif path == "/ep1.mp3":
                send_audio(h, self.audio, ranges=True)
            elif path == "/ep2.mp3":
                send_audio(h, self.audio, ranges=False)
            elif path == "/page.html":
                body = b"<html>not audio</html>"
                h.send_response(200)
                h.send_header("Content-Type", "text/html")
                h.send_header("Content-Length", str(len(body)))
                h.end_headers()
                h.wfile.write(body)
            elif path == "/art.png":
                h.send_response(200)
                h.send_header("Content-Type", "image/png")
                h.send_header("Content-Length", str(len(self.art)))
                h.end_headers()
                h.wfile.write(self.art)
            else:
                h.send_response(404)
                h.send_header("Content-Length", "0")
                h.end_headers()

        return respond
