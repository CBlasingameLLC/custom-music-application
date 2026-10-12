"""Smoke-test the desktop app: launch the real thing and drive its window.

    python scripts/smoke_desktop.py --music tests/fixtures -- "C:\\...\\Music Toolkit.exe"

Everything after `--` is the command that starts the app (an installed
Music Toolkit.exe, or `electron .` in development). The script appends
`--remote-debugging-port`, attaches over the Chrome DevTools Protocol and
checks what no unit test can: the backend came up, the page accepted the
per-launch token, the desktop bridge is exactly what the UI expects, audio
streams and starts playing inside the window, foreign pages get nothing from
the bridge, a song handed to the running app by a second launch ("Open with")
is added and played, closing the window while music plays leaves the app in the
tray until it is told to quit, the window opens where it was left, a song named on
the command line is played, and quitting takes the backend down with it.

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
import urllib.error
import urllib.request
from pathlib import Path

WINDOWS = sys.platform == "win32"
# CI consoles on Windows default to a legacy code page; the checks print arrows and quotes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BRIDGE = [
    "desktopInfo", "mediaKeys", "onMediaKey", "onOpenFiles", "openExternal", "openPath", "pathForFile", "quit", "selectFile",
    "selectFolder", "setCloseAction", "setNowPlaying", "showItemInFolder", "takeOpenFiles", "update",
]
SEEDED_WINDOW = {"x": 60, "y": 40, "width": 900, "height": 600, "maximized": False, "trayNoticeShown": True}
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


# Windows: list the windows the app's process has on screen and, when asked, send WM_CLOSE (what the close button does) to each visible
# app window. Not Process.CloseMainWindow(): that picks one window by a rule of its own, and when it picked the wrong one (or refused)
# nothing could tell why. The list is the evidence when the app does not close.
WINDOWS_WINDOWS = r"""
param([int]$ProcessId, [int]$Send)
Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class MtkWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc p, IntPtr l);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr h);
  [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr h, uint cmd);
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static string[] Windows(uint pid, bool send) {
    var found = new List<string>();
    EnumWindows((h, l) => {
      uint owner; GetWindowThreadProcessId(h, out owner);
      if (owner != pid) return true;
      var cls = new StringBuilder(256); GetClassName(h, cls, 256);
      var title = new StringBuilder(256); GetWindowText(h, title, 256);
      bool visible = IsWindowVisible(h);
      RECT r; GetWindowRect(h, out r);
      found.Add(string.Format("0x{0:X}|{1}|{2}|visible={3}|enabled={4}|minimized={5}|owner=0x{6:X}|rect={7},{8},{9},{10}",
        h.ToInt64(), cls, title, visible, IsWindowEnabled(h), IsIconic(h), GetWindow(h, 4).ToInt64(), r.L, r.T, r.R, r.B));
      if (send && visible && cls.ToString().StartsWith("Chrome_WidgetWin")) PostMessage(h, 0x10, IntPtr.Zero, IntPtr.Zero);
      return true;
    }, IntPtr.Zero);
    return found.ToArray();
  }
}
"@
[MtkWin]::Windows([uint32]$ProcessId, [bool]$Send) | ForEach-Object { "window: $_" }
$p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if ($p) {
  $main = '0x{0:X}' -f $p.MainWindowHandle.ToInt64()
  "process: pid=$($p.Id) responding=$($p.Responding) mainWindowHandle=$main mainWindowTitle=$($p.MainWindowTitle)"
} else { "process: gone" }
"""
WINDOW_LOG: list[str] = []  # what the last look at the app's windows found, for a failure report


def app_windows(process: subprocess.Popen, send: bool) -> list[str]:
    """Windows only: one line per window the app's process has (handle|class|title|visible|enabled|minimized|owner|rect) and a line
    about the process; with `send`, each visible app window is also asked to close."""
    script = Path(tempfile.gettempdir()) / "mtk-app-windows.ps1"
    script.write_text(WINDOWS_WINDOWS, encoding="utf-8")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-ProcessId", str(process.pid), "-Send", "1" if send else "0"],
        capture_output=True, text=True, errors="replace",  # a window title may hold characters the console cannot show
    )
    WINDOW_LOG[:] = [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    return list(WINDOW_LOG)


def describe_windows(process: subprocess.Popen, when: str) -> None:
    """Print what the app has on screen (Windows only; on Linux the window manager is the one to ask)."""
    if WINDOWS:
        print(f"  info  the app's windows {when}:", flush=True)
        for line in app_windows(process, send=False):
            print(f"          {line}", flush=True)


def press_close_button(process: subprocess.Popen) -> bool:
    """Ask the app's window to close the way its close button does: a polite request the app may answer by hiding the
    window instead (window.close() from the page, or the debugger's Browser.close, would quit without asking). True if
    the request was sent; on Linux it needs python-xlib (and an X server), which only a developer's machine has."""
    if WINDOWS:
        sent = app_windows(process, send=True)
        if not any(line.startswith(("window:", "process:")) for line in sent):
            # The listing itself failed (what it said is in WINDOW_LOG): fall back to the plain request, so the check still means something.
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {process.pid}).CloseMainWindow()"], capture_output=True, text=True, errors="replace",
            )
            return "True" in result.stdout
        return any(line.startswith("window:") and "|visible=True|" in line and "Chrome_WidgetWin" in line for line in sent)
    try:
        from Xlib import X, display, protocol
    except ImportError:
        return False
    screen = display.Display()
    delete, protocols, owner = (screen.intern_atom(name) for name in ("WM_DELETE_WINDOW", "WM_PROTOCOLS", "_NET_WM_PID"))
    sent = False

    def visit(window) -> None:
        nonlocal sent
        try:
            pids = window.get_full_property(owner, X.AnyPropertyType)
        except Exception:
            pids = None
        if pids is not None and process.pid in [int(pid) for pid in pids.value]:
            event = protocol.event.ClientMessage(window=window, client_type=protocols, data=(32, [delete, X.CurrentTime, 0, 0, 0]))
            window.send_event(event, event_mask=X.NoEventMask)
            sent = True
        for child in window.query_tree().children:
            visit(child)

    visit(screen.screen().root)
    screen.sync()
    screen.close()
    return sent


def app_page_url(debug_port: int, process: subprocess.Popen):
    """The address of the app's window once it has one (it carries the launch token), "exited" if the app died first."""
    if process.poll() is not None:
        return "exited"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=2) as response:
            pages = json.load(response)
    except OSError:
        return None
    urls = [p["url"] for p in pages if p["url"].startswith("http://127.0.0.1:") and "token=" in p["url"]]
    return urls[0] if urls else None


def backend_up(backend_port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{backend_port}/", timeout=2)
    except urllib.error.HTTPError:
        return True  # 401 without the token: the server answered
    except OSError:
        return False
    return True


def tracks_in_library(page) -> int:
    return page.evaluate("fetch('/api/about').then(r => r.json()).then(a => a.tracks)")


def player_source(page) -> str:
    return page.evaluate("import('/static/js/player.js').then((m) => m.player.get().source)")


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
    profile, music, elsewhere, userdata = scratch / "profile", scratch / "music", scratch / "elsewhere", scratch / "userdata"
    for folder in (profile, music, elsewhere, userdata):
        folder.mkdir()
    mp3s = sorted(Path(args.music).glob("*.mp3"))
    if not mp3s:
        sys.exit(f"no .mp3 files in {args.music}")
    for mp3 in mp3s:
        shutil.copy(mp3, music / mp3.name)
    # Two songs that are not in the library: one handed to the running app, one the app is started with.
    handed, started_with = elsewhere / "Handed over.mp3", elsewhere / "Started with.mp3"
    for copy in (handed, started_with):
        shutil.copy(mp3s[0], copy)
    # Where the window was left last time (the app must open there; it also proves the file is read).
    window_file = userdata / "window-state.json"
    window_file.write_text(json.dumps(SEEDED_WINDOW))

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
            [*command, f"--remote-debugging-port={debug_port}", f"--user-data-dir={userdata}"],
            cwd=args.cwd or scratch, env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs,
        )

    backend_port = None
    try:
        step("launch")

        page_url = wait_for(lambda: app_page_url(debug_port, process), args.launch_timeout)
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
            size = page.evaluate("[window.outerWidth, window.outerHeight]")
            check(abs(size[0] - SEEDED_WINDOW["width"]) <= 60 and abs(size[1] - SEEDED_WINDOW["height"]) <= 60,
                  f"the window opened at the size it was left ({SEEDED_WINDOW['width']}x{SEEDED_WINDOW['height']}; it measures {size})")

            step("desktop bridge")
            keys = page.evaluate("Object.keys(window.mtk || {}).sort()")
            check(keys == BRIDGE, f"window.mtk is exactly {BRIDGE} (got {keys})")
            check(page.evaluate("typeof require") == "undefined" and page.evaluate("typeof process") == "undefined",
                  "no Node.js globals reach the page")

            step("media keys")
            keys_state = page.evaluate("window.mtk.mediaKeys()")
            check(isinstance(keys_state, dict) and sorted(keys_state) == ["next", "playpause", "previous", "stop"] and all(isinstance(v, bool) for v in keys_state.values()),
                  f"the app reports which media keys it claimed ({keys_state})")

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

            if args.expect_mtp_helper:
                step("phones and players")
                listing = page.evaluate("fetch('/api/devices/mtp?fresh=true').then(r => r.json())")
                check(listing.get("available") is True, f"the installed app found its helper for phones and players ({listing.get('reason')})")
                check(not listing.get("error"), f"the helper started and looked for devices ({listing.get('error')})")
                check(listing.get("devices") == [], f"no phone is plugged into this machine ({listing.get('devices')})")

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

            step("a song handed to the running app")
            before = tracks_in_library(page)
            second_log = scratch / "second-launch.log"
            with open(second_log, "w") as second_out:
                second = subprocess.Popen([*command, f"--user-data-dir={userdata}", str(handed)], cwd=args.cwd or scratch, env=env,
                                          stdout=second_out, stderr=subprocess.STDOUT, **kwargs)
            try:
                check(wait_for(lambda: second.poll() is not None, 40) is not None, "a second launch hands the song over and exits (one app per machine)")
            finally:
                if second.poll() is None:
                    kill_tree(second)
            check(bool(wait_for(lambda: tracks_in_library(page) == before + 1, 30, 0.5)), "the song it was given joined the library")
            check(bool(wait_for(lambda: player_source(page) == "Opened files", 30, 0.5)), "and is playing, as an opened file")

            step("the bridge refuses other pages")
            page.goto(stranger_url)
            check(page.evaluate("typeof window.mtk") == "object", "the preload is attached to the foreign page (so the next check means something)")
            started = time.time()
            answers = page.evaluate(
                "(async () => [await window.mtk.selectFile({}), await window.mtk.selectFolder(), "
                "await window.mtk.openPath('.'), await window.mtk.showItemInFolder('.'), "
                "await window.mtk.update.status(), await window.mtk.update.check(), await window.mtk.update.install(), "
                "await window.mtk.update.setAuto(false), await window.mtk.mediaKeys(), "
                "await window.mtk.setNowPlaying({title: 'x', playing: true}), await window.mtk.setCloseAction('quit'), "
                "await window.mtk.takeOpenFiles(), await window.mtk.desktopInfo(), await window.mtk.quit()])()")
            check(answers == [None] * 14 and time.time() - started < 5, f"no dialog opens, nothing updates, nothing quits, and every call answers null ({answers})")
            check(process.poll() is None and backend_up(backend_port), "the app is still running after a foreign page asked it to quit")
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

            step("closing the window")
            info = page.evaluate("window.mtk.desktopInfo()")
            check(isinstance(info, dict) and sorted(info) == ["closeAction", "hidden", "tray"], f"the shell reports its tray and what closing does ({info})")
            check(info.get("closeAction") == "tray_if_playing", f"closing keeps the music going by default ({info.get('closeAction')!r})")
            if args.expect_tray:
                check(info.get("tray") is True, "the tray icon exists")
            if not wait_for(lambda: page.evaluate("import('/static/js/player.js').then((m) => m.player.get().playing)"), 3):
                page.evaluate("location.hash = '#/songs'")
                page.wait_for_selector(".trow:not(.skeleton)", timeout=20000)
                row = page.locator(".trow", has_text="Smoke Tone")
                (row if row.count() else page.locator(".trow")).first.locator(".t1").dblclick()
            check(bool(wait_for(lambda: page.evaluate("import('/static/js/player.js').then((m) => m.player.get().playing)"), 30)), "music is playing")
            # Tell the shell what is playing; it needs a moment to hear it (the page sends it as the song starts).
            time.sleep(1.0)
            pressed = press_close_button(process)
            if not pressed:
                print("  skip  the window's close button cannot be pressed from here (needs PowerShell on Windows, or python-xlib under X)")
                for line in WINDOW_LOG:
                    print(f"          {line}", flush=True)
            else:
                check(True, "the window's close button was pressed")
            if args.expect_tray:
                check(pressed, "the window's close button can be pressed on this machine")
            if not pressed:
                try:
                    page.evaluate("window.mtk.quit()")
                except Exception:
                    pass
            elif info.get("tray"):
                time.sleep(2.5)
                check(process.poll() is None, "closing the window while music plays leaves the app running in the tray")
                check(backend_up(backend_port), "the backend is still up")
                hidden = wait_for(lambda: page.evaluate("window.mtk.desktopInfo()").get("hidden"), 10)
                check(bool(hidden), "and the window is hidden")
                try:
                    page.evaluate("window.mtk.quit()")  # what the tray's Quit and Settings' Quit button do
                except Exception:
                    pass  # the app may go before the answer arrives
            else:
                check(bool(wait_for(lambda: process.poll() is not None, 30)), "with no tray icon, closing the window quits the app")
        exited = wait_for(lambda: process.poll() is not None, 30)
        check(bool(exited), "the app exited when it was told to quit")
        check(bool(wait_for(lambda: not backend_up(backend_port), 15)), "the backend process is gone, not orphaned")

        step("where the window was left")
        saved = json.loads(window_file.read_text()) if window_file.exists() else {}
        check(abs(saved.get("width", 0) - SEEDED_WINDOW["width"]) <= 60 and abs(saved.get("height", 0) - SEEDED_WINDOW["height"]) <= 60,
              f"closing wrote the window's size to {window_file.name} ({saved})")
        check(saved.get("maximized") is False, "and that it was not maximized")

        step("relaunch: the app is started with a song")
        debug_port = free_port()
        with open(log_path, "a") as log:
            process = subprocess.Popen(
                [*command, f"--remote-debugging-port={debug_port}", f"--user-data-dir={userdata}", str(started_with)],
                cwd=args.cwd or scratch, env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs,
            )
        page_url = wait_for(lambda: app_page_url(debug_port, process), args.launch_timeout)
        check(bool(page_url) and page_url != "exited", "the window opened again")
        if page_url and page_url != "exited":
            backend_port = int(page_url.split("://", 1)[1].split("/", 1)[0].split(":")[1])
            closed = False
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
                page = next(pg for pg in browser.contexts[0].pages if pg.url.startswith("http://127.0.0.1:"))
                page.wait_for_selector(".sidebar", timeout=30000)
                size = page.evaluate("[window.outerWidth, window.outerHeight]")
                check(abs(size[0] - SEEDED_WINDOW["width"]) <= 60 and abs(size[1] - SEEDED_WINDOW["height"]) <= 60, f"it opened at the remembered size ({size})")
                check(bool(wait_for(lambda: player_source(page) == "Opened files", 40, 0.5)), "the song it was started with is playing, as an opened file")
                check(tracks_in_library(page) == len(mp3s) + 2, f"the library kept the song handed over earlier and took the new one ({tracks_in_library(page)})")
                page.evaluate("window.mtk.setCloseAction('quit')")
                chosen = page.evaluate("window.mtk.desktopInfo()")
                check(chosen.get("closeAction") == "quit", f"the shell holds the choice to quit ({chosen})")
                describe_windows(process, "before the close button is pressed")
                print(f"  info  the page's view of its window (visibilityState, hasFocus): {page.evaluate('[document.visibilityState, document.hasFocus()]')}", flush=True)
                pressed = press_close_button(process)
                if args.expect_tray:
                    check(pressed, "the window's close button can be pressed on this machine")
                if WINDOWS:
                    print("  info  the close request went to:", flush=True)
                    for line in WINDOW_LOG:
                        print(f"          {line}", flush=True)
                if not pressed:
                    try:
                        page.evaluate("window.mtk.quit()")  # no way to press it here (a developer's machine without python-xlib)
                    except Exception:
                        pass
                closed = bool(wait_for(lambda: process.poll() is not None, 20))
                if not closed:
                    # Say what the app looks like from outside and from inside, and whether it can quit at all (the check below fails
                    # either way: closing the window was supposed to do it).
                    describe_windows(process, "20 s after the close button was pressed")
                    try:
                        print(f"  info  the page still answers: {page.evaluate('window.mtk.desktopInfo()')}", flush=True)
                    except Exception as error:
                        print(f"  info  the page no longer answers: {str(error)[:200]}", flush=True)
                    if WINDOWS:  # the plain .NET way of asking, which picks the window by its own rule
                        plain = subprocess.run(
                            ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {process.pid}).CloseMainWindow()"], capture_output=True, text=True, errors="replace",
                        )
                        again = bool(wait_for(lambda: process.poll() is not None, 8))
                        print(f"  info  Process.CloseMainWindow() answered {plain.stdout.strip()!r}; after it the app {'quit' if again else 'was still running'}", flush=True)
                    try:
                        page.evaluate("window.mtk.quit()")
                    except Exception:
                        pass
                    quit_works = bool(wait_for(lambda: process.poll() is not None, 10))
                    print(f"  info  asked to quit through the bridge, it {'quit' if quit_works else 'did not quit either'}", flush=True)
            check(closed, "closing the window quits when that is what was chosen")
            check(bool(wait_for(lambda: not backend_up(backend_port), 15)), "and the backend is gone")
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
            for name in ("desktop.log", "backend.log"):
                log_file = profile / ".musictoolkit" / "logs" / name
                if log_file.exists():
                    print(f"--- {name} ---")
                    print(log_file.read_text(errors="replace")[-3000:])
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--music", required=True, help="folder with a few .mp3 files to scan and play")
    parser.add_argument("--cwd", help="working directory for the app command (e.g. desktop/ for `electron .`)")
    parser.add_argument("--launch-timeout", type=float, default=90, help="seconds to wait for the window")
    parser.add_argument("--strict-audio", action="store_true", help="also require the playback clock to advance (needs an audio device or a fake sink)")
    parser.add_argument("--expect-tray", action="store_true", help="the machine has a notification area: the app must create its tray icon and keep running when closed while playing")
    parser.add_argument("--expect-updater", action="store_true", help="the app is an installed build: its updater must be active")
    parser.add_argument("--expect-mtp-helper", action="store_true", help="the app is an installed build: the helper for phones and players must be there and start")
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
