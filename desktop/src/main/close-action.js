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

/**
 * The close button's setting as the main process holds it. It starts as the default, becomes what the backend has stored once
 * that has been read, and becomes what the page says whenever the person changes it: a choice made by the page is never undone
 * by the stored value arriving late (reading it takes a moment, and the page may speak first).
 */
function createCloseSetting(initial = DEFAULT_ACTION) {
  let value = normalizeAction(initial);
  let chosen = false;
  return {
    get: () => value,
    /** The person's choice, from the page. */
    choose(next) {
      chosen = true;
      value = normalizeAction(next);
      return value;
    },
    /** The backend's stored value, read at start-up; ignored if the page has already chosen. */
    loaded(next) {
      if (!chosen) value = normalizeAction(next);
      return value;
    },
  };
}

/** What the page says is playing, cleaned up: short plain strings and booleans, nothing else gets through. */
function cleanNowPlaying(info) {
  const text = (value) => (typeof value === 'string' ? value.slice(0, 200) : '');
  const source = info && typeof info === 'object' ? info : {};
  const title = text(source.title);
  return { title, artist: text(source.artist), playing: Boolean(source.playing) && Boolean(title), hasTrack: Boolean(title) };
}

module.exports = { ACTIONS, DEFAULT_ACTION, normalizeAction, shouldKeepRunning, createCloseSetting, cleanNowPlaying };
