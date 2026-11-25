"""What Settings tells someone about installing the app on a phone or a Mac (static/js/install.js, run under node: it is plain functions)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from musictoolkit.web.app import STATIC_DIR

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
import fs from 'node:fs';
const source = fs.readFileSync(process.argv[2], 'utf8');
const { installAdvice, openedAsApp } = await import('data:text/javascript,' + encodeURIComponent(source));

const out = { advice: {}, opened: {} };
const where = (protocol, hostname, standalone = false) => installAdvice({ protocol, hostname, standalone });
for (const [name, args] of Object.entries({
  tailnet: ['https:', 'musicpi.tail1234.ts.net'],
  lan_http: ['http:', '192.168.1.20'],
  lan_name_http: ['http:', 'musicpi.local'],
  loopback: ['http:', '127.0.0.1'],
  localhost: ['http:', 'localhost'],
  subdomain_of_localhost: ['http:', 'music.localhost'],
  ipv6_loopback: ['http:', '[::1]'],
  https_loopback: ['https:', 'localhost'],
  lookalike: ['https:', 'notlocalhost.example'],
  installed: ['https:', 'musicpi.tail1234.ts.net', true],
})) out.advice[name] = where(...args);

for (const [name, [navigatorValue, matches]] of Object.entries({
  ios_home_screen: [true, false], ios_browser: [false, false], dock_or_chrome_app: [undefined, true], plain_tab: [undefined, false], none: [undefined, undefined],
})) {
  Object.defineProperty(globalThis, 'navigator', { value: { standalone: navigatorValue }, configurable: true, writable: true }); // (newer node has its own, with only a getter)
  globalThis.window = { matchMedia: matches === undefined ? undefined : (query) => ({ matches: query === '(display-mode: standalone)' && matches }) };
  out.opened[name] = openedAsApp();
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict:
    harness = tmp_path_factory.mktemp("install") / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(Path(STATIC_DIR) / "js" / "install.js")], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_an_address_that_only_works_on_the_computer_it_is_typed_on_is_said_to_be_so(results: dict) -> None:
    advice = results["advice"]
    for name in ("loopback", "localhost", "subdomain_of_localhost", "ipv6_loopback", "https_loopback"):
        assert advice[name]["local"] is True, name
    for name in ("tailnet", "lan_http", "lan_name_http", "lookalike", "installed"):
        assert advice[name]["local"] is False, name  # (a name that merely ends like localhost is not one)


def test_only_https_is_secure(results: dict) -> None:
    advice = results["advice"]
    assert [name for name, got in advice.items() if got["secure"]] == ["tailnet", "https_loopback", "lookalike", "installed"]


def test_installed_is_what_the_page_is_told_and_nothing_else(results: dict) -> None:
    advice = results["advice"]
    assert [name for name, got in advice.items() if got["installed"]] == ["installed"]


def test_a_page_opened_from_an_icon_knows_it_whichever_way_the_browser_says_so(results: dict) -> None:
    assert results["opened"] == {
        "ios_home_screen": True, "ios_browser": False, "dock_or_chrome_app": True, "plain_tab": False, "none": False,
    }
