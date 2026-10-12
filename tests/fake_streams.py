"""A fake server for radio and podcast tests: whatever the test scripts, sent over a real local socket.

    def script(h):                      # h is the http.server request handler: h.path, h.headers, h.send_response(), h.wfile ...
        h.send_response(200)
        h.send_header("Content-Type", "audio/mpeg")
        h.end_headers()
        h.wfile.write(b"...")

    with serve_script(script) as server:
        fetch(server.url + "/stream")           # http://127.0.0.1:<port>
        server.requests                          # [{"path": "/stream", "headers": {...}}]

Answers are HTTP/1.0 (the body ends when the connection closes), which is how most internet radio servers behave. A client that goes away
mid-stream makes the script's write fail; scripts that loop should stop on OSError.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace


@contextmanager
def serve_script(script: Callable[[BaseHTTPRequestHandler], None], host: str = "127.0.0.1") -> Iterator[SimpleNamespace]:
    log: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (the name http.server looks for)
            log.append({"path": self.path, "headers": dict(self.headers)})
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
