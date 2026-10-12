'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { DEFAULT_SIZE, MIN_SIZE, parseState, fitToDisplays, loadState, saveState, createSaver } = require('../src/main/window-state.js');

const SCREEN = { x: 0, y: 0, width: 1920, height: 1040 };
const SECOND = { x: 1920, y: 0, width: 1280, height: 1024 };
const LEFT = { x: -1280, y: 0, width: 1280, height: 1024 };

test('only sensible saved values are kept', () => {
  assert.deepEqual(parseState('{"x":10.4,"y":-20,"width":900,"height":700,"maximized":true,"trayNoticeShown":false}'),
    { x: 10, y: -20, width: 900, height: 700, maximized: true, trayNoticeShown: false });
  assert.deepEqual(parseState('{"x":"10","y":null,"width":NaN,"maximized":"yes","other":1}'), {});
  assert.deepEqual(parseState('{"width":800}'), { width: 800 });
  for (const junk of ['', 'not json', 'null', '[1,2]', '"text"', '12']) assert.deepEqual(parseState(junk), {}, junk);
});

test('a window that is wholly on a display opens exactly where it was', () => {
  assert.deepEqual(fitToDisplays({ x: 100, y: 80, width: 1000, height: 700 }, [SCREEN]), { x: 100, y: 80, width: 1000, height: 700 });
});

test('nothing saved opens at the default size and lets the system choose the place', () => {
  assert.deepEqual(fitToDisplays({}, [SCREEN]), DEFAULT_SIZE);
  assert.deepEqual(fitToDisplays({ width: 1000 }, [SCREEN]), { width: 1000, height: DEFAULT_SIZE.height });
  assert.deepEqual(fitToDisplays({ x: 5, y: 5 }, [SCREEN]), { x: 5, y: 5, width: DEFAULT_SIZE.width, height: DEFAULT_SIZE.height });
});

test('a window hanging over an edge is moved back inside the display it mostly sits on', () => {
  const fitted = fitToDisplays({ x: 1500, y: 900, width: 1000, height: 700 }, [SCREEN]);
  assert.deepEqual(fitted, { x: 920, y: 340, width: 1000, height: 700 });
  const above = fitToDisplays({ x: 100, y: -300, width: 1000, height: 700 }, [SCREEN]);
  assert.equal(above.y, 0);
});

test('a monitor that is no longer attached does not strand the window off-screen', () => {
  const stranded = fitToDisplays({ x: 2200, y: 100, width: 1000, height: 700 }, [SCREEN]);
  assert.deepEqual(stranded, { width: 1000, height: 700 }, 'only the size survives; the system places it');
  assert.deepEqual(fitToDisplays({ x: 2200, y: 100, width: 1000, height: 700 }, [SCREEN, SECOND]), { x: 2200, y: 100, width: 1000, height: 700 }, 'but with the monitor attached it returns');
  assert.deepEqual(fitToDisplays({ x: -900, y: 50, width: 800, height: 600 }, [LEFT, SCREEN]), { x: -900, y: 50, width: 800, height: 600 }, 'a monitor to the left has negative coordinates');
});

test('just a sliver on a display is not enough to reach the window', () => {
  assert.deepEqual(fitToDisplays({ x: 1900, y: 100, width: 1000, height: 700 }, [SCREEN]), { width: 1000, height: 700 });
  assert.notDeepEqual(fitToDisplays({ x: 1700, y: 100, width: 1000, height: 700 }, [SCREEN]), { width: 1000, height: 700 });
});

test('a window lying across two displays settles on the one with most of it', () => {
  const mostlyOnSecond = fitToDisplays({ x: 1700, y: 100, width: 1000, height: 700 }, [SCREEN, SECOND]);
  assert.deepEqual(mostlyOnSecond, { x: 1920, y: 100, width: 1000, height: 700 }, 'moved wholly onto the second display');
  const mostlyOnFirst = fitToDisplays({ x: 1000, y: 100, width: 1000, height: 700 }, [SCREEN, SECOND]);
  assert.deepEqual(mostlyOnFirst, { x: 920, y: 100, width: 1000, height: 700 }, 'moved wholly onto the first');
});

test('a window too small is raised to the minimum and one too big is shrunk to the display', () => {
  assert.deepEqual(fitToDisplays({ x: 50, y: 50, width: 200, height: 100 }, [SCREEN]), { x: 50, y: 50, ...MIN_SIZE });
  const huge = fitToDisplays({ x: 0, y: 0, width: 5000, height: 4000 }, [SCREEN]);
  assert.deepEqual(huge, { x: 0, y: 0, width: SCREEN.width, height: SCREEN.height });
});

test('unusable display lists are ignored', () => {
  assert.deepEqual(fitToDisplays({ x: 10, y: 10, width: 900, height: 600 }, []), { width: 900, height: 600 });
  assert.deepEqual(fitToDisplays({ x: 10, y: 10, width: 900, height: 600 }, null), { width: 900, height: 600 });
  assert.deepEqual(fitToDisplays({ x: 10, y: 10, width: 900, height: 600 }, [{ x: 'a' }, null, { x: 0, y: 0, width: 0, height: 5 }]), { width: 900, height: 600 });
});

test('the state survives a round trip through a file, and a damaged file is simply ignored', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mtk-window-'));
  const file = path.join(dir, 'deep', 'window-state.json');
  try {
    assert.deepEqual(loadState(file), {}, 'nothing saved yet');
    assert.equal(saveState(file, { x: 1, y: 2, width: 900, height: 600, maximized: true }), true);
    assert.deepEqual(loadState(file), { x: 1, y: 2, width: 900, height: 600, maximized: true });
    assert.deepEqual(fs.readdirSync(path.dirname(file)), ['window-state.json'], 'no temporary file is left behind');
    fs.writeFileSync(file, '{"x": 1,');
    assert.deepEqual(loadState(file), {});
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('a state that cannot be written is reported, never thrown', () => {
  const broken = { mkdirSync() { throw new Error('read-only'); } };
  assert.equal(saveState('/nowhere/state.json', {}, broken), false);
});

function fakeTimers() {
  const timers = { pending: [], cleared: 0 };
  return {
    timers,
    setTimer: (fn, ms) => { const timer = { fn, ms }; timers.pending.push(timer); return timer; },
    clearTimer: (timer) => { timers.cleared += 1; timers.pending = timers.pending.filter((t) => t !== timer); },
  };
}

test('moving the window saves once, shortly after it stops', () => {
  const saved = [];
  const clock = fakeTimers();
  let bounds = { x: 1, y: 1 };
  const saver = createSaver({ file: 'f', read: () => bounds, save: (file, state) => saved.push([file, state]), ...clock });
  saver.schedule();
  bounds = { x: 5, y: 5 };
  saver.schedule();
  saver.schedule();
  assert.equal(clock.timers.pending.length, 1, 'one timer waiting, the earlier ones cancelled');
  assert.equal(clock.timers.pending[0].ms, 500);
  clock.timers.pending[0].fn();
  assert.deepEqual(saved, [['f', { x: 5, y: 5 }]], 'the latest place is what gets written');
});

test('closing the window saves at once and cancels the wait', () => {
  const saved = [];
  const clock = fakeTimers();
  const saver = createSaver({ file: 'f', read: () => ({ x: 9 }), save: (file, state) => saved.push(state), ...clock });
  saver.schedule();
  saver.flush();
  assert.deepEqual(saved, [{ x: 9 }]);
  assert.equal(clock.timers.pending.length, 0);
});

test('nothing is written when there is nothing to read', () => {
  const saved = [];
  const saver = createSaver({ file: 'f', read: () => null, save: (...args) => saved.push(args), ...fakeTimers() });
  saver.flush();
  assert.deepEqual(saved, []);
});
