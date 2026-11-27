"""The service worker's behaviour, with its file run under node against a stand-in for the browser's worker scope.

It must do three things and nothing else: pass a page that the server answers through as it is (a refusal and a sign-in page too), say
that the server cannot be reached when a page load fails or gets no answer, and leave everything that is not a page load alone. It must
never keep anything (no Cache Storage: a stored copy of a signed-in page, or of an address with a token in it, is what it must not be).
The page it passes on is, where the browser can do it, the request that the browser itself started (navigation preload): a request that
the worker makes by itself is taken for another one by some browsers, which can leave a SameSite cookie out of it.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from musictoolkit import __version__
from musictoolkit.web.app import STATIC_DIR

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
import vm from 'node:vm';
import fs from 'node:fs';

const source = fs.readFileSync(process.argv[2], 'utf8').replace('__VERSION__', process.argv[3]);

/** Run the worker's file in a scope that has what a worker has, and return the handlers it set and what it asked of the scope. */
function load({ fetchImpl, timer, registration }) {
  const handlers = {};
  const touched = [];
  const calls = { skipWaiting: 0, claim: 0, timers: [] };
  const scope = {
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting: () => { calls.skipWaiting += 1; },
    clients: { claim: async () => { calls.claim += 1; } },
    location: { origin: 'https://music.example' },
    ...(registration ? { registration } : {}),
  };
  const sandbox = {
    self: scope, Response, Promise, Error, Headers, URL,
    fetch: fetchImpl,
    setTimeout: (fn, ms) => { calls.timers.push(ms); return timer ? timer(fn, ms) : 0; },
  };
  for (const name of ['caches', 'indexedDB', 'localStorage', 'XMLHttpRequest', 'importScripts']) {
    Object.defineProperty(sandbox, name, { get() { touched.push(name); return undefined; } });
  }
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return { handlers, touched, calls };
}

/** What a fetch event makes the worker answer: { intercepted, response } (response is what respondWith was given, awaited). */
async function ask(worker, request, preloadResponse) {
  let given;
  worker.handlers.fetch({ request, preloadResponse, respondWith: (value) => { given = value; } });
  if (given === undefined) return { intercepted: false };
  const response = await given;
  return { intercepted: true, response };
}

const summary = async (answer) => answer.intercepted
  ? { intercepted: true, status: answer.response.status, type: answer.response.headers.get('content-type'), cache: answer.response.headers.get('cache-control'), body: await answer.response.clone().text() }
  : { intercepted: false };

const navigation = { mode: 'navigate', method: 'GET', url: 'https://music.example/' };
const out = {};

// the server answers: the page, a refusal, a sign-in page are passed on as they are
for (const status of [200, 401, 403, 500]) {
  const sent = new Response(`from the server ${status}`, { status });
  const worker = load({ fetchImpl: async () => sent });
  const answer = await ask(worker, navigation);
  out[`answers_${status}`] = { same: answer.response === sent, status: answer.response.status, text: await answer.response.clone().text(), timers: worker.calls.timers };
}

// the server cannot be reached: the connection fails
{
  const worker = load({ fetchImpl: async () => { throw new TypeError('Failed to fetch'); } });
  out.unreachable = await summary(await ask(worker, navigation));
}

// the server does not answer: the wait runs out (the timer fires at once here)
{
  const worker = load({ fetchImpl: () => new Promise(() => {}), timer: (fn) => queueMicrotask(fn) });
  out.silent = await summary(await ask(worker, navigation));
  out.silent_waited = worker.calls.timers;
}

// a server that answers late but within the wait is not given up on: the timer never fires
{
  const sent = new Response('late but there', { status: 200 });
  const worker = load({ fetchImpl: () => new Promise((resolve) => setImmediate(() => resolve(sent))), timer: () => 0 });
  const answer = await ask(worker, navigation);
  out.late = { same: answer.response === sent };
}

// the browser has started the page's request (navigation preload): its answer is the page, and the worker asks for nothing more
{
  const sent = new Response('the browser asked', { status: 200 });
  let fetched = 0;
  const worker = load({ fetchImpl: async () => { fetched += 1; return new Response('the worker asked'); } });
  const answer = await ask(worker, navigation, Promise.resolve(sent));
  out.preloaded = { same: answer.response === sent, fetched, timers: worker.calls.timers };
}

// no request was started early (the promise says nothing, or there is no such promise): the worker fetches the page itself
{
  const sent = new Response('the worker asked', { status: 200 });
  const unused = await ask(load({ fetchImpl: async () => sent }), navigation, Promise.resolve(undefined));
  const absent = await ask(load({ fetchImpl: async () => sent }), navigation, undefined);
  out.preload_unused = { same: unused.response === sent, absent: absent.response === sent };
}

// the request that was started early fails: the server cannot be reached, and the worker does not ask again
{
  let fetched = 0;
  const worker = load({ fetchImpl: async () => { fetched += 1; return new Response('x'); } });
  out.preload_failed = { ...(await summary(await ask(worker, navigation, Promise.reject(new TypeError('Failed to fetch'))))), fetched };
}

// the request that was started early gets no answer: the wait runs out
{
  const worker = load({ fetchImpl: async () => new Response('x'), timer: (fn) => queueMicrotask(fn) });
  out.preload_silent = await summary(await ask(worker, navigation, new Promise(() => {})));
}

// what is not a page load is not touched
{
  const worker = load({ fetchImpl: async () => { throw new Error('the worker must not fetch for these'); } });
  out.not_intercepted = {};
  for (const [name, request] of Object.entries({
    script: { mode: 'cors', method: 'GET', url: 'https://music.example/static/js/main.js' },
    api: { mode: 'cors', method: 'GET', url: 'https://music.example/api/tracks' },
    audio: { mode: 'no-cors', method: 'GET', url: 'https://music.example/api/tracks/1/stream', headers: new Headers({ range: 'bytes=0-' }) },
    image: { mode: 'no-cors', method: 'GET', url: 'https://music.example/api/art/1' },
    form: { mode: 'navigate', method: 'POST', url: 'https://music.example/signin' },
    subresource: { mode: 'same-origin', method: 'GET', url: 'https://music.example/' },
  })) out.not_intercepted[name] = (await ask(worker, request)).intercepted;
}

// installing and activating: take over at once, and keep nothing
{
  const worker = load({ fetchImpl: async () => new Response('x') });
  const routes = [];
  worker.handlers.install({ addRoutes: (rule) => routes.push(rule) });
  const bare = load({ fetchImpl: async () => new Response('x') });
  bare.handlers.install({}); // (a browser that cannot be told: nothing to call, and nothing breaks)
  let waited;
  worker.handlers.activate({ waitUntil: (value) => { waited = value; } });
  await waited;
  out.lifecycle = { skipWaiting: worker.calls.skipWaiting, claim: worker.calls.claim, events: Object.keys(worker.handlers).sort(), routes, bareSkipWaiting: bare.calls.skipWaiting };
}

// taking over: the browser is asked to start the page's request early where it can be asked, and the worker takes over whatever the browser says
{
  let enabled = 0;
  const waitFor = async (worker) => {
    let waited;
    worker.handlers.activate({ waitUntil: (value) => { waited = value; } });
    await waited;
  };
  const offered = load({ fetchImpl: async () => new Response('x'), registration: { navigationPreload: { enable: async () => { enabled += 1; } } } });
  const refused = load({ fetchImpl: async () => new Response('x'), registration: { navigationPreload: { enable: async () => { throw new Error('not allowed'); } } } });
  const without = load({ fetchImpl: async () => new Response('x'), registration: {} });
  const bare = load({ fetchImpl: async () => new Response('x') });
  for (const worker of [offered, refused, without, bare]) await waitFor(worker);
  out.preload_enabled = { enabled, offered: offered.calls.claim, refused: refused.calls.claim, without: without.calls.claim, bare: bare.calls.claim };
}

// across all of it, the worker never reached for storage or for anything but fetch
{
  const worker = load({ fetchImpl: async () => new Response('x') });
  await ask(worker, navigation);
  await ask(worker, { mode: 'cors', method: 'GET', url: 'https://music.example/api/x' });
  out.touched = worker.touched;
}

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict:
    folder = tmp_path_factory.mktemp("sw")
    harness = folder / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(harness), str(STATIC_DIR / "sw.js"), __version__], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("status", [200, 401, 403, 500])
def test_a_page_the_server_answers_is_passed_on_as_it_is_whatever_the_status(results: dict, status: int) -> None:
    got = results[f"answers_{status}"]
    assert got["same"] is True and got["status"] == status and got["text"] == f"from the server {status}"
    assert got["timers"] == [8000]  # (the wait before giving up: 8 seconds, set once per page load)


def test_a_page_load_that_fails_shows_that_the_server_cannot_be_reached(results: dict) -> None:
    got = results["unreachable"]
    assert got["intercepted"] and got["status"] == 200 and got["type"] == "text/html; charset=utf-8" and got["cache"] == "no-store"
    assert "Can't reach your music server" in got["body"] and "Tailscale" in got["body"]
    assert 'href="/"' in got["body"] and "location.reload()" in got["body"]  # (the button: try again, on the same address)
    assert f"Music Toolkit {__version__}" in got["body"]
    assert "viewport" in got["body"] and "color-scheme" in got["body"]


def test_a_server_that_does_not_answer_is_given_up_on_after_eight_seconds(results: dict) -> None:
    assert results["silent"]["status"] == 200 and "Can't reach your music server" in results["silent"]["body"]
    assert results["silent_waited"] == [8000]


def test_a_server_that_answers_late_but_in_time_is_not_given_up_on(results: dict) -> None:
    assert results["late"]["same"] is True


def test_the_page_is_the_answer_to_the_request_the_browser_started_for_it_and_the_worker_asks_for_nothing_more(results: dict) -> None:
    assert results["preloaded"] == {"same": True, "fetched": 0, "timers": [8000]}


def test_where_no_request_was_started_early_the_worker_fetches_the_page_itself(results: dict) -> None:
    assert results["preload_unused"] == {"same": True, "absent": True}


def test_a_request_started_early_that_fails_shows_that_the_server_cannot_be_reached_and_is_not_made_again(results: dict) -> None:
    got = results["preload_failed"]
    assert got["intercepted"] and got["status"] == 200 and "Can't reach your music server" in got["body"] and got["fetched"] == 0


def test_a_request_started_early_that_gets_no_answer_is_given_up_on(results: dict) -> None:
    assert results["preload_silent"]["intercepted"] and "Can't reach your music server" in results["preload_silent"]["body"]


def test_it_asks_the_browser_to_start_the_pages_request_and_takes_over_whatever_the_browser_says(results: dict) -> None:
    assert results["preload_enabled"] == {"enabled": 1, "offered": 1, "refused": 1, "without": 1, "bare": 1}


def test_only_a_page_load_is_taken_over(results: dict) -> None:
    """Scripts, the API, songs (with Range), covers, a form posted and a request that is not a navigation go straight to the network."""
    assert results["not_intercepted"] == {name: False for name in ("script", "api", "audio", "image", "form", "subresource")}


def test_it_installs_and_takes_over_at_once_and_listens_for_nothing_else(results: dict) -> None:
    assert results["lifecycle"] == {
        "skipWaiting": 1, "claim": 1, "events": ["activate", "fetch", "install"], "bareSkipWaiting": 1,
        # everything but a page load is sent to the network without asking the worker (Chromium loses a request sent as the page closes otherwise)
        "routes": [{"condition": {"not": {"requestMode": "navigate"}}, "source": "network"}],
    }


def test_it_keeps_nothing(results: dict) -> None:
    """No Cache Storage (so no copy of a signed-in page, or of an address with a token in it, in the browser), no database, no local storage."""
    assert results["touched"] == []
    source = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))
    for word in ("caches", "indexedDB", "localStorage", "importScripts", "XMLHttpRequest", "postMessage"):
        assert word not in code, word  # (navigation preload keeps nothing either: the browser's own request for the page, passed on)


def test_the_worker_file_is_small(results: dict) -> None:
    assert (STATIC_DIR / "sw.js").stat().st_size < 4096  # (it is all there is to read: keep it that way)
