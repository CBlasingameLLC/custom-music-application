"""Smoke-test the desktop app: launch the real thing and drive its window.

    python scripts/smoke_desktop.py --music tests/fixtures -- "C:\\...\\Music Toolkit.exe"

Everything after `--` is the command that starts the app (an installed
Music Toolkit.exe, or `electron .` in development). The script appends
`--remote-debugging-port`, attaches over the Chrome DevTools Protocol and
checks what no unit test can: the backend came up, the page accepted the
per-launch token, the desktop bridge is exactly what the UI expects, audio
streams and starts playing inside the window, foreign pages get nothing from
the bridge, and closing the window takes the backend down with it.

Needs `pip install playwright` (only its CDP client is used; no browser is
downloaded). Runs on a throwaway user profile.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

WINDOWS = sys.platform == "win32"
# CI consoles on Windows default to a legacy code page; the checks print arrows and quotes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BRIDGE = ["onMediaKey", "openExternal", "openPath", "selectFile", "selectFolder", "showItemInFolder", "update"]
UPDATE_BRIDGE = ["check", "install", "onStatus", "setAuto", "status"]
failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + message, flush=True)
    if not ok:
        failures.append(message)


def step(name: str) -> None:
    print(f"\n== {name}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_tree(process: subprocess.Popen) -> None:
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        subprocess.run(["taskkill", "/IM", "mtk-backend.exe", "/T", "/F"], capture_output=True)
    else:
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        subprocess.run("pkill -f '[d]ashboard --port' || true", shell=True)


def wait_for(predicate, seconds: float, interval: float = 0.25):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def start_stranger() -> tuple[http.server.ThreadingHTTPServer, list[str]]:
    """A web server on another origin: it must never receive a request from the app window."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><title>stranger</title><body>another origin</body></html>")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


def drive(args, command: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    scratch = Path(tempfile.mkdtemp(prefix="mtk-desktop-smoke-"))
    profile, music = scratch / "profile", scratch / "music"
    for folder in (profile, music):
        folder.mkdir()
    mp3s = sorted(Path(args.music).glob("*.mp3"))
    if not mp3s:
        sys.exit(f"no .mp3 files in {args.music}")
    for mp3 in mp3s:
        shutil.copy(mp3, music / mp3.name)

    if WINDOWS:
        stray = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Music Toolkit.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True)
        print("running before launch:", stray.stdout.strip() or "(nothing)", flush=True)
        for image in ("Music Toolkit.exe", "mtk-backend.exe"):
            subprocess.run(["taskkill", "/IM", image, "/T", "/F"], capture_output=True)
        time.sleep(1)

    debug_port = free_port()
    env = {
        **os.environ,
        "HOME": str(profile),  # the app keeps its data in ~/.musictoolkit
        "USERPROFILE": str(profile),
        "ELECTRON_ENABLE_LOGGING": "1",  # Chromium's own log lines into the captured output
    }
    log_path = scratch / "app.log"
    stranger, stranger_hits = start_stranger()
    stranger_url = f"http://127.0.0.1:{stranger.server_address[1]}/"
    with open(log_path, "w") as log:
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
        # Electron finds its user-data folder through the OS (not APPDATA), and the single-instance lock lives
        # there: give this run its own, so an instance already running (the installer may have started one)
        # cannot make the app quit at once.
        process = subprocess.Popen(
            [*command, f"--remote-debugging-port={debug_port}", f"--user-data-dir={scratch / 'userdata'}"],
            cwd=args.cwd or scratch, env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs,
        )

    backend_port = None
    try:
        step("launch")

        def app_page_url():
            if process.poll() is not None:
                return "exited"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=2) as response:
                    pages = json.load(response)
            except OSError:
                return None
            urls = [p["url"] for p in pages if p["url"].startswith("http://127.0.0.1:") and "token=" in p["url"]]
            return urls[0] if urls else None

        page_url = wait_for(app_page_url, args.launch_timeout)
        check(bool(page_url) and page_url != "exited", "the window opened on the backend with a launch token")
        if not page_url or page_url == "exited":
            print(f"app process: {'exited with code ' + str(process.returncode) if process.poll() is not None else 'still running, no window'}")
            if WINDOWS:
                print(subprocess.run(["tasklist", "/FI", "IMAGENAME eq Music Toolkit.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout)
                events = (
                    "Get-WinEvent -FilterHashtable @{LogName='Application'; Level=1,2; StartTime=(Get-Date).AddMinutes(-10)} "
                    "-MaxEvents 6 -ErrorAction SilentlyContinue | Format-List TimeCreated, ProviderName, Message"
                )
                print("recent application errors:")
                print(subprocess.run(["powershell", "-NoProfile", "-Command", events], capture_output=True, text=True).stdout[-3000:])
            return
        backend_port = int(page_url.split("://", 1)[1].split("/", 1)[0].split(":")[1])

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            context = browser.contexts[0]
            page = next(pg for pg in context.pages if pg.url.startswith("http://127.0.0.1:"))
            problems: list[str] = []
            # A song without cover art answers its image request with 404; the UI shows a placeholder.
            page.on("console", lambda m: problems.append(m.text) if m.type == "error" and "status of 404" not in m.text else None)
            page.on("pageerror", lambda e: problems.append(str(e)))

            page.wait_for_selector(".sidebar", timeout=30000)
            check(True, "the interface rendered (the token was accepted and the session cookie set)")
            check(page.evaluate("document.title") == "Music Toolkit", "window title")

            step("desktop bridge")
            keys = page.evaluate("Object.keys(window.mtk || {}).sort()")
            check(keys == BRIDGE, f"window.mtk is exactly {BRIDGE} (got {keys})")
            check(page.evaluate("typeof require") == "undefined" and page.evaluate("typeof process") == "undefined",
                  "no Node.js globals reach the page")

            step("app updates")
            check(page.evaluate("Object.keys(window.mtk.update).sort()") == UPDATE_BRIDGE, f"window.mtk.update is exactly {UPDATE_BRIDGE}")
            update = page.evaluate("window.mtk.update.status()")
            version = page.evaluate("fetch('/api/about').then(r => r.json()).then(a => a.version)")
            check(update.get("current") == version, f"the app reports its own version ({update.get('current')} vs the backend's {version})")
            if args.expect_updater:
                check(update.get("state") != "disabled", f"the updater is active in the installed app (state {update.get('state')!r})")
                checked = page.evaluate("window.mtk.update.check()")
                # No newer release (or no update feed yet, or no network on the runner) are all fine here;
                # what matters is that electron-updater loaded and ran a check without crashing the app.
                check(checked.get("state") in ("up-to-date", "error", "downloading", "ready", "checking"),
                      f"a check for updates ran ({checked.get('state')!r}: {checked.get('error')})")

            step("first run, library, playback")
            check(page.locator("text=Add your music").count() == 1, "an empty library shows the first-run prompt")
            status = page.evaluate(
                "fetch('/api/library/roots', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: %s})}).then(r => r.status)"
                % json.dumps(str(music)))
            check(status in (200, 201), f"add the music folder (HTTP {status})")
            total = wait_for(lambda: page.evaluate("fetch('/api/about').then(r => r.json()).then(a => a.tracks)") == len(mp3s), 60, 0.5)
            check(bool(total), f"the scan found {len(mp3s)} songs")
            page.evaluate("location.hash = '#/songs'")
            page.wait_for_selector(".trow:not(.skeleton)", timeout=20000)
            # Prefer the long tone fixture: the others last a fraction of a second.
            row = page.locator(".trow", has_text="Smoke Tone")
            (row if row.count() else page.locator(".trow")).first.locator(".t1").dblclick()
            playing = wait_for(lambda: page.get_by_role("button", name="Pause").count() > 0, 30)
            check(bool(playing), "the song streams and starts playing (cookie auth, Range requests, decoding)")
            if args.strict_audio and playing:
                first = page.locator(".seek .time").first.inner_text()
                advanced = wait_for(lambda: page.locator(".seek .time").first.inner_text() != first, 8)
                check(bool(advanced), "the playback position advances")
            check(page.evaluate("document.title").startswith("▶ "), f"window title follows the song ({page.evaluate('document.title')!r})")

            step("the bridge refuses other pages")
            page.goto(stranger_url)
            check(page.evaluate("typeof window.mtk") == "object", "the preload is attached to the foreign page (so the next check means something)")
            started = time.time()
            answers = page.evaluate(
                "(async () => [await window.mtk.selectFile({}), await window.mtk.selectFolder(), "
                "await window.mtk.openPath('.'), await window.mtk.showItemInFolder('.'), "
                "await window.mtk.update.status(), await window.mtk.update.check(), await window.mtk.update.install(), "
                "await window.mtk.update.setAuto(false)])()")
            check(answers == [None] * 8 and time.time() - started < 5, f"no dialog opens, nothing updates, and every call answers null ({answers})")
            page.goto(page_url)
            page.wait_for_selector(".sidebar", timeout=30000)

            step("arguments are validated")
            answers = page.evaluate("(async () => [await window.mtk.openPath('/no/such/dir'), await window.mtk.openPath(123), await window.mtk.showItemInFolder(42)])()")
            check(answers == [None, None, None], f"nonsense arguments are ignored ({answers})")

            if not args.skip_external:
                step("navigation stays inside the app")
                before = len(stranger_hits)
                page.evaluate(f"setTimeout(() => {{ location.href = {json.dumps(stranger_url + 'blocked')} }}, 0)")
                time.sleep(1.5)
                check(page.url.startswith(f"http://127.0.0.1:{backend_port}/"), "a page-initiated navigation to another site is blocked")
                page.evaluate(f"window.open({json.dumps(stranger_url + 'popup')})")
                time.sleep(1.5)
                check(len(context.pages) == 1, "window.open does not create a window")
                check(len(stranger_hits) == before, "the other site never received a request from the window")

            check(not problems, f"no errors in the page console ({problems[:3]})")

            step("closing the window stops everything")
            session = context.new_cdp_session(page)
            try:
                session.send("Browser.close")
            except Exception:
                pass
        exited = wait_for(lambda: process.poll() is not None, 30)
        check(bool(exited), "the app exited when its window closed")

        def backend_gone():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{backend_port}/", timeout=2)
            except OSError:
                return True
            except Exception:
                return False
            return False

        check(bool(wait_for(backend_gone, 15)), "the backend process is gone, not orphaned")
    except Exception as error:
        failures.append(f"unexpected error: {error!r}")
        raise
    finally:
        kill_tree(process)
        stranger.shutdown()
        time.sleep(0.5)
        if failures:
            print("\n--- app output ---")
            print(log_path.read_text(errors="replace")[-3000:])
            backend_log = profile / ".musictoolkit" / "logs" / "backend.log"
            if backend_log.exists():
                print("--- backend.log ---")
                print(backend_log.read_text(errors="replace")[-3000:])
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--music", required=True, help="folder with a few .mp3 files to scan and play")
    parser.add_argument("--cwd", help="working directory for the app command (e.g. desktop/ for `electron .`)")
    parser.add_argument("--launch-timeout", type=float, default=90, help="seconds to wait for the window")
    parser.add_argument("--strict-audio", action="store_true", help="also require the playback clock to advance (needs an audio device or a fake sink)")
    parser.add_argument("--expect-updater", action="store_true", help="the app is an installed build: its updater must be active")
    parser.add_argument("--skip-external", action="store_true", help="skip the checks that make the app open a link in the system browser")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- followed by the command that starts the app")
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("give the command that starts the app after --")
    drive(args, command)
    if failures:
        print(f"\nFAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("\nDesktop smoke test passed.")


if __name__ == "__main__":
    main()
