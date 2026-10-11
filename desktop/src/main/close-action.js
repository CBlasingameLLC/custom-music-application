'use strict';

// What the window's close button does. The setting lives in the backend's config (app.close_action); the main process
// keeps a copy because it must decide at once, while the window is closing.

const ACTIONS = ['quit', 'tray_if_playing', 'tray'];
const DEFAULT_ACTION = 'tray_if_playing';

const normalizeAction = (value) => (ACTIONS.includes(value) ? value : DEFAULT_ACTION);

/** Keep the app running (hidden, in the tray) instead of closing it? */
function shouldKeepRunning(action, playing) {
  const chosen = normalizeAction(action);
  return chosen === 'tray' || (chosen === 'tray_if_playing' && Boolean(playing));
}

/** What the page says is playing, cleaned up: short plain strings and booleans, nothing else gets through. */
function cleanNowPlaying(info) {
  const text = (value) => (typeof value === 'string' ? value.slice(0, 200) : '');
  const source = info && typeof info === 'object' ? info : {};
  const title = text(source.title);
  return { title, artist: text(source.artist), playing: Boolean(source.playing) && Boolean(title), hasTrack: Boolean(title) };
}

module.exports = { ACTIONS, DEFAULT_ACTION, normalizeAction, shouldKeepRunning, cleanNowPlaying };
