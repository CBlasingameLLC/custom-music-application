"""What the browser tests of signing in share: a server that can be given Tailscale's headers, a recorder of what it was sent, and a page of another site."""

from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from tests.conftest import build_web
from tests.e2e.conftest import TOKEN


class Recorder:
    """Writes down what the server was sent and what it answered, for each call to the API: (method, path, status, Sec-Fetch-Site, whether
    the request carried the login Tailscale writes). A browser does not let a page read the headers it adds itself, so the server does."""

    def __init__(self, app, log: list) -> None:
        self.app, self.log = app, log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = {key.decode(): value.decode() for key, value in scope["headers"]}

        async def sending(message):
            if message["type"] == "http.response.start":
                self.log.append((scope["method"], scope["path"], message["status"], headers.get("sec-fetch-site", ""), "tailscale-user-login" in headers))
            await send(message)

        await self.app(scope, receive, sending)


def serve_app(tmp_path: Path, **options) -> SimpleNamespace:
    """The app on a real port (`options` go to create_app: tailscale_logins ...), with its recorder."""
    web = build_web(tmp_path, None, token=TOKEN, login_days=90, **options)
    log: list = []
    web.app.add_middleware(Recorder, log=log)
    server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        assert time.time() < deadline, "the test server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    return SimpleNamespace(base=f"http://127.0.0.1:{port}", port=port, server=server, thread=thread, ctx=web.ctx, log=log)


def stop_app(served: SimpleNamespace) -> None:
    served.server.should_exit = True
    served.thread.join(timeout=10)


class Elsewhere:
    """A page of another site (another host name, as far as a browser can tell) that asks the server for things."""

    def __init__(self, target: str) -> None:
        body = f"""<!doctype html><title>elsewhere</title><script>
        const target = {target!r};
        window.done = (async () => {{
          await fetch(target + '/api/about', {{ mode: 'no-cors', credentials: 'include' }});
          await fetch(target + '/api/library/scan', {{ method: 'POST', mode: 'no-cors', credentials: 'include' }});
          return true;
        }})();
        </script>""".encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://localhost:{self.http.server_address[1]}/"  # (`localhost`, not 127.0.0.1: a different site)

    def stop(self) -> None:
        self.http.shutdown()
        self.thread.join(timeout=5)
