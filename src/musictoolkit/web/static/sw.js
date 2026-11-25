// The service worker of the installed app (Add to Home Screen on a phone, Add to Dock on a Mac).
//
// It does two things. Its being there is what lets a browser install the page as an app. And when the page cannot be loaded because the
// server cannot be reached (the phone is not on the tailnet, the Pi is off), it shows a page that says so, with a button to try again,
// instead of the browser's own error.
//
// It keeps nothing: no cache, so no old copy of the app to get out of step with the server, and nothing of a signed-in page (or of an
// address with a token in it) stored in the browser. Only the page itself is looked at; scripts, the API, songs and covers go straight
// to the network, as they would without it.
//
// The word below is replaced with the app's version when the server sends this file, so that a new version of the app is a new file
// and the browser installs it.
const VERSION = '__VERSION__';
const WAIT = 8000; // milliseconds a page waits for the server before this says it cannot be reached

self.addEventListener('install', (event) => {
  self.skipWaiting();
  // A page that a worker with a fetch handler controls has every one of its requests offered to the worker first, and in Chromium a
  // request sent as the page closes (where an episode got to) is lost on the way. Where the browser can be told, everything but a page
  // load goes to the network without the worker being asked at all.
  if (typeof event.addRoutes === 'function') event.addRoutes({ condition: { not: { requestMode: 'navigate' } }, source: 'network' });
});
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

const OFFLINE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light"><title>Music Toolkit</title>
<style>
:root { color-scheme: dark light; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; font: 16px/1.5 system-ui, -apple-system, sans-serif; background: #0d0f14; color: #eceef4; }
@media (prefers-color-scheme: light) { body { background: #f5f6fb; color: #151826; } }
main { max-width: 26rem; padding: 2rem 1.25rem; }
h1 { margin: 0 0 .5rem; font-size: 1.5rem; }
p { margin: .6rem 0; opacity: .85; }
a.button { display: inline-block; margin-top: 1rem; padding: .7rem 1.4rem; border-radius: 999px; background: #6c57ff; color: #fff; font-weight: 600; text-decoration: none; }
</style></head>
<body><main>
<h1>Can't reach your music server</h1>
<p>This device did not get an answer from it. If the server is at home, check that this device is on your tailnet (the Tailscale app says it is connected), and that the server is running.</p>
<a class="button" href="/" onclick="location.reload(); return false">Try again</a>
<p style="font-size:.85rem;opacity:.6">Music Toolkit ${VERSION}</p>
</main></body></html>`;

const unreachable = () =>
  new Response(OFFLINE, { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } });

/** The page from the server, or the page that says it cannot be reached. A server that answers at all (a refusal, a sign-in page) is passed on as it is. */
function page(request) {
  const waiting = new Promise((_, giveUp) => setTimeout(() => giveUp(new Error('no answer')), WAIT));
  return Promise.race([fetch(request), waiting]).catch(unreachable);
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.mode === 'navigate' && request.method === 'GET') event.respondWith(page(request));
});
