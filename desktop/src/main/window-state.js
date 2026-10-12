'use strict';

// Where the window was, so it opens in the same place and size next time. Plain functions with the file system and the
// timers passed in, so they are tested without a screen (test/window-state.test.js).

const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_SIZE = { width: 1360, height: 880 };
const MIN_SIZE = { width: 720, height: 520 };
const MIN_VISIBLE = { width: 160, height: 100 }; // how much of the window must be on a display for it to be within reach

const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

/** What was saved ({x, y, width, height, maximized, trayNoticeShown}), keeping only what makes sense; {} if unusable. */
function parseState(text) {
  let raw;
  try {
    raw = JSON.parse(text);
  } catch {
    return {};
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const state = {};
  for (const key of ['x', 'y', 'width', 'height']) if (isNumber(raw[key])) state[key] = Math.round(raw[key]);
  for (const key of ['maximized', 'trayNoticeShown']) if (typeof raw[key] === 'boolean') state[key] = raw[key];
  return state;
}

const overlap = (a, b) => ({
  width: Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x),
  height: Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y),
});

const clamp = (value, low, high) => Math.min(Math.max(value, low), Math.max(low, high));

/**
 * Where to open the window. `workAreas` are the usable rectangles ({x, y, width, height}) of the displays attached now:
 * a monitor that has been unplugged since must not strand the window off-screen. The saved place is used when enough of
 * the window would be on a display, and is moved back inside that display if it hangs over an edge; otherwise only the
 * size is returned and the system chooses the place.
 */
function fitToDisplays(state, workAreas, defaults = DEFAULT_SIZE) {
  const areas = (Array.isArray(workAreas) ? workAreas : []).filter(
    (a) => a && [a.x, a.y, a.width, a.height].every(isNumber) && a.width > 0 && a.height > 0,
  );
  const size = {
    width: Math.max(MIN_SIZE.width, isNumber(state.width) ? state.width : defaults.width),
    height: Math.max(MIN_SIZE.height, isNumber(state.height) ? state.height : defaults.height),
  };
  if (!isNumber(state.x) || !isNumber(state.y) || !areas.length) return size;

  const wanted = { x: state.x, y: state.y, ...size };
  let best = null;
  let bestArea = 0;
  for (const area of areas) {
    const shared = overlap(wanted, area);
    if (shared.width < Math.min(MIN_VISIBLE.width, size.width) || shared.height < Math.min(MIN_VISIBLE.height, size.height)) continue;
    if (shared.width * shared.height > bestArea) {
      best = area;
      bestArea = shared.width * shared.height;
    }
  }
  if (!best) return size;

  const width = Math.min(size.width, best.width);
  const height = Math.min(size.height, best.height);
  return {
    x: clamp(wanted.x, best.x, best.x + best.width - width),
    y: clamp(wanted.y, best.y, best.y + best.height - height),
    width,
    height,
  };
}

function loadState(file, fileSystem = fs) {
  try {
    return parseState(fileSystem.readFileSync(file, 'utf8'));
  } catch {
    return {};
  }
}

/** Write the state so that a crash in the middle never leaves a half-written file behind. Never throws. */
function saveState(file, state, fileSystem = fs) {
  try {
    fileSystem.mkdirSync(path.dirname(file), { recursive: true });
    const temp = `${file}.tmp`;
    fileSystem.writeFileSync(temp, JSON.stringify(state));
    fileSystem.renameSync(temp, file);
    return true;
  } catch {
    return false; // a state that cannot be saved must never stop the app
  }
}

/**
 * Saves while the window is being dragged or resized without writing for every pixel: `schedule()` saves shortly after
 * the last change, `flush()` saves now (when the window closes). `read()` gives the state to write.
 */
function createSaver({ file, read, save = saveState, delay = 500, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let timer = null;
  const flush = () => {
    if (timer !== null) clearTimer(timer);
    timer = null;
    const state = read();
    if (state) save(file, state);
  };
  const schedule = () => {
    if (timer !== null) clearTimer(timer);
    timer = setTimer(flush, delay);
  };
  return { schedule, flush };
}

module.exports = { DEFAULT_SIZE, MIN_SIZE, MIN_VISIBLE, parseState, fitToDisplays, loadState, saveState, createSaver };
