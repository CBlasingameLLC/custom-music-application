"""A small HTTP server that stands in for an online service (lrclib.net, ListenBrainz, ...) in tests.

    with serve({"/get": (200, {"id": 1}), "/search": lambda query: (200, [])}) as service:
        talk_to(service.url)          # http://127.0.0.1:<port>
        service.requests[0]["query"]  # what was asked: {"artist_name": "..."}

A route is a (status, JSON body) pair or a function of the query returning one; any path it does not know answers 404
with an empty body. `service.requests` records every request (path, query, headers) in order. `service.redirects` maps
a path to another path the service sends the client on to (302), to see whether a client follows it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

Route = tuple[int, Any] | Callable[[dict[str, str]], tuple[int, Any]]


class Service:
    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = dict(routes)
        self.requests: list[dict[str, Any]] = []
        self.redirects: dict[str, str] = {}
        self.url = ""

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r["path"] == path)


@contextmanager
def serve(routes: dict[str, Route] | None = None) -> Iterator[Service]:
    service = Service(routes or {})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (the name http.server looks for)
            parts = urlsplit(self.path)
            query = dict(parse_qsl(parts.query))
            service.requests.append({"path": parts.path, "query": query, "headers": dict(self.headers)})
            if parts.path in service.redirects:
                self.send_response(302)
                self.send_header("Location", service.url + service.redirects[parts.path])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            route = service.routes.get(parts.path) or service.routes.get(parts.path.rstrip("/"))
            status, body = (route(query) if callable(route) else route) if route is not None else (404, None)
            payload = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:  # keep the test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    service.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
