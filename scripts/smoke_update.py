"""Prove the whole update chain on a real Windows install, without GitHub.

    python scripts/smoke_update.py --old MusicToolkit-Setup-0.0.1.exe --feed-dir new --install-dir C:\\tmp\\mtk --expect-version 0.3.0

An old build is installed, started with MTK_UPDATE_FEED pointing at a local web server that serves the
newer release's files (latest.yml, installer, blockmap), asked to check for updates over the desktop
bridge, and left to download the new installer. Then it is told to restart and install. Passing means
the installed files really became the new version and the app came back up by itself: the same steps a
person's installed copy takes when a release is published, minus the trip to GitHub.

Needs `pip install playwright` (only its CDP client is used). Windows only; takes a few minutes.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_desktop import check, failures, free_port, kill_tree, step, wait_for  # noqa: E402


class Feed(http.server.SimpleHTTPRequestHandler):
    """Serves the release files. Logs what the app asks for, since a missing file is the usual failure."""

    requests: list[str] = []

    def log_message(self, fmt, *args):  # noqa: A003 - http.server API
        Feed.requests.append(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


def installed_version(exe: Path) -> str:
    """The version stamped on the exe, as semver. Windows keeps four parts (0.3.0.0); the app and latest.yml say 0.3.0."""
    command = f"(Get-Item -LiteralPath '{exe}').VersionInfo.ProductVersion"
    raw = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True).stdout.strip()
    return ".".join(raw.split(".")[:3])


def running(image: str) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return image.lower() in out.lower()


def stop_all() -> None:
    for image in ("Music Toolkit.exe", "mtk-backend.exe"):
        subprocess.run(["taskkill", "/IM", image, "/T", "/F"], capture_output=True)


def run(args) -> None:
    from playwright.sync_api import sync_playwright

    scratch = Path(tempfile.mkdtemp(prefix="mtk-update-"))
    profile = scratch / "profile"
    profile.mkdir()
    install_dir = Path(args.install_dir)
    exe = install_dir / "Music Toolkit.exe"

    feed_port = free_port()
    handler = functools.partial(Feed, directory=str(Path(args.feed_dir).resolve()))
    feed = http.server.ThreadingHTTPServer(("127.0.0.1", feed_port), handler)
    threading.Thread(target=feed.serve_forever, daemon=True).start()
    feed_url = f"http://127.0.0.1:{feed_port}/"

    step("install the old build")
    stop_all()
    setup = subprocess.run([str(Path(args.old).resolve()), "/S", f"/D={install_dir}"], timeout=300)
    time.sleep(5)  # an installer can start the app a moment after it exits
    stop_all()
    time.sleep(1)
    check(exe.exists(), f"the old build installed ({setup.returncode})")
    if not exe.exists():
        return
    old_version = installed_version(exe)
    check(old_version != args.expect_version, f"the installed build is older than the update ({old_version} < {args.expect_version})")

    step("start it with a local update feed")
    debug_port = free_port()
    env = {**os.environ, "HOME": str(profile), "USERPROFILE": str(profile), "MTK_UPDATE_FEED": feed_url}
    log_path = scratch / "app.log"
    with open(log_path, "w") as log:
        process = subprocess.Popen(
            [str(exe), f"--remote-debugging-port={debug_port}", f"--user-data-dir={scratch / 'userdata'}"],
            env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    try:
        def page_url():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=2) as response:
                    pages = json.load(response)
            except OSError:
                return None
            urls = [p["url"] for p in pages if p["url"].startswith("http://127.0.0.1:") and "token=" in p["url"]]
            return urls[0] if urls else None

        check(bool(wait_for(page_url, 120)), "the old build's window opened")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            page = next(pg for pg in browser.contexts[0].pages if pg.url.startswith("http://127.0.0.1:"))
            page.wait_for_selector(".sidebar", timeout=60000)

            step("check for updates and download the new build")
            first = page.evaluate("window.mtk.update.status()")
            check(first["state"] != "disabled" and first["current"] == old_version, f"the updater is active in the old build ({first})")
            page.evaluate("window.mtk.update.check()")
            seen: list[str] = []

            def settled():
                status = page.evaluate("window.mtk.update.status()")
                if not seen or seen[-1] != status["state"]:
                    seen.append(status["state"])
                    print(f"        update state: {status['state']} {status.get('version') or ''} {status.get('percent') or ''} {status.get('error') or ''}", flush=True)
                return status if status["state"] in ("ready", "error", "up-to-date") else None

            status = wait_for(settled, args.download_timeout, 1.0)
            check(bool(status) and status["state"] == "ready" and status["version"] == args.expect_version,
                  f"the new version was found and downloaded ({status})")
            if not status or status["state"] != "ready":
                return

            step("restart into the new build")
            try:
                page.evaluate("window.mtk.update.install()")
            except Exception:
                pass  # the window goes away while evaluating: that is the point

        def relaunched():
            return running("Music Toolkit.exe") and installed_version(exe) == args.expect_version

        done = wait_for(relaunched, args.install_timeout, 2.0)
        check(bool(done), f"the app was replaced and started again as {args.expect_version} (files say {installed_version(exe)})")
        check(bool(wait_for(lambda: running("mtk-backend.exe"), 60, 1.0)), "the new build's backend came up")
        check((install_dir / "resources" / "mtk-backend.exe").exists(), "the new build's files are in place")
    except Exception as error:
        failures.append(f"unexpected error: {error!r}")
        raise
    finally:
        kill_tree(process)
        stop_all()
        feed.shutdown()
        print("\n--- what the app asked the feed for ---")
        print("\n".join(Feed.requests[-30:]) or "(nothing)")
        if failures:
            updater_log = profile / ".musictoolkit" / "logs" / "updater.log"
            print("\n--- updater.log ---")
            print(updater_log.read_text(errors="replace")[-4000:] if updater_log.exists() else "(none)")
            print("--- app output ---")
            print(log_path.read_text(errors="replace")[-2000:])
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old", required=True, help="the older installer to install first")
    parser.add_argument("--feed-dir", required=True, help="folder holding the newer release's latest.yml, installer and blockmap")
    parser.add_argument("--install-dir", required=True, help="where to install")
    parser.add_argument("--expect-version", required=True, help="the version the update should arrive as")
    parser.add_argument("--download-timeout", type=float, default=300)
    parser.add_argument("--install-timeout", type=float, default=240)
    args = parser.parse_args()
    if sys.platform != "win32":
        sys.exit("this test needs Windows")
    run(args)
    if failures:
        print(f"\nFAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("\nUpdate smoke test passed.")


if __name__ == "__main__":
    main()
