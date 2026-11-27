// The key that gets this device its sign-in back when its browser has lost the cookie (see remember.py; the sign-in page's script, restore.js, is what uses it).
import { api } from './lib.js';

export const KEY_NAME = 'mtk.remember.v1'; // (restore.js reads the same name)
export const RENEW_WITHIN = 30 * 86400; // seconds: a key with less than this left to run is replaced by a new one

/** The key this device keeps, `{ key, expires }` (`expires`: Unix seconds), or null (none, or storage that is blocked or holds something else). */
export function savedKey() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY_NAME) || 'null');
    return saved && typeof saved.key === 'string' && saved.key && Number.isFinite(saved.expires) ? saved : null;
  } catch {
    return null;
  }
}

function keep(saved) {
  try {
    localStorage.setItem(KEY_NAME, JSON.stringify({ key: saved.key, expires: saved.expires }));
  } catch {
    /* storage that cannot be written: there is no key to keep, and the cookie is all there is */
  }
}

export function forgetKey() {
  try {
    localStorage.removeItem(KEY_NAME);
  } catch {
    /* storage that cannot be written cannot be cleared either */
  }
}

/**
 * Called as the app starts: a device that is signed in with the token keeps a key that has more than 30 days left (the server makes a new key each time it is asked,
 * so the key of a device that is in use never runs out). A server that does not keep devices signed in (the desktop app) is no place for a key. Says nothing when
 * something goes wrong: no server that answers, an older server, storage that is blocked.
 */
export async function keepSignedIn(now = () => Date.now() / 1000) {
  try {
    const session = await api('/session');
    if (!(session.remember_days > 0)) {
      forgetKey();
      return;
    }
    if (session.by !== 'token') return;
    const saved = savedKey();
    if (saved && saved.expires - now() > RENEW_WITHIN) return;
    keep(await api('/session/remember', { method: 'POST' }));
  } catch {
    /* nothing to keep */
  }
}

/** Sign this device out: the key is thrown away first (so that a device that is half-way out cannot sign itself in again), then the cookie, then the page asks for the token. */
export async function signOutHere() {
  forgetKey();
  await api('/session/signout', { method: 'POST' });
  location.replace('/');
}
