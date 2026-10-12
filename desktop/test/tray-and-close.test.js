'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { MAX_TOOLTIP, tooltipText, trayMenuTemplate } = require('../src/main/tray-menu.js');
const { ACTIONS, DEFAULT_ACTION, createCloseSetting, normalizeAction, shouldKeepRunning, cleanNowPlaying } = require('../src/main/close-action.js');

test('the tooltip names the app, or what is playing or paused', () => {
  assert.equal(tooltipText(), 'Music Toolkit');
  assert.equal(tooltipText({}), 'Music Toolkit');
  assert.equal(tooltipText({ title: 'Glacier', artist: 'Aurora Vale', playing: true }), 'Playing: Glacier - Aurora Vale');
  assert.equal(tooltipText({ title: 'Glacier', playing: false }), 'Paused: Glacier');
});

test('a long title is cut to what Windows will show', () => {
  const text = tooltipText({ title: 'T'.repeat(500), artist: 'A', playing: true });
  assert.equal(text.length, MAX_TOOLTIP);
  assert.ok(text.endsWith('...') && MAX_TOOLTIP < 127);
});

test('the tray menu offers the window, the transport and quit', () => {
  const calls = [];
  const actions = Object.fromEntries(['toggleWindow', 'playPause', 'next', 'previous', 'quit'].map((name) => [name, () => calls.push(name)]));
  const idle = trayMenuTemplate({ windowVisible: true, playing: false, hasTrack: false }, actions);
  assert.deepEqual(idle.map((item) => item.label || item.type), ['Hide Music Toolkit', 'separator', 'Play', 'Next', 'Previous', 'separator', 'Quit Music Toolkit']);
  assert.deepEqual(idle.filter((item) => 'enabled' in item).map((item) => item.enabled), [false, false, false], 'nothing to control until a song is queued');

  const playing = trayMenuTemplate({ windowVisible: false, playing: true, hasTrack: true }, actions);
  assert.deepEqual(playing.map((item) => item.label).filter(Boolean), ['Show Music Toolkit', 'Pause', 'Next', 'Previous', 'Quit Music Toolkit']);
  assert.ok(playing.filter((item) => 'enabled' in item).every((item) => item.enabled));
  for (const item of playing) item.click?.();
  assert.deepEqual(calls, ['toggleWindow', 'playPause', 'next', 'previous', 'quit']);
});

test('what the close button does follows the setting, and an unknown setting means the default', () => {
  assert.deepEqual(ACTIONS, ['quit', 'tray_if_playing', 'tray']);
  assert.equal(DEFAULT_ACTION, 'tray_if_playing');
  for (const junk of [undefined, null, '', 'Quit', 'explode', 3, {}]) assert.equal(normalizeAction(junk), DEFAULT_ACTION);
  assert.equal(normalizeAction('quit'), 'quit');

  assert.equal(shouldKeepRunning('quit', true), false);
  assert.equal(shouldKeepRunning('quit', false), false);
  assert.equal(shouldKeepRunning('tray', false), true);
  assert.equal(shouldKeepRunning('tray', true), true);
  assert.equal(shouldKeepRunning('tray_if_playing', true), true);
  assert.equal(shouldKeepRunning('tray_if_playing', false), false);
  assert.equal(shouldKeepRunning('nonsense', true), true, 'the default keeps playing music going');
  assert.equal(shouldKeepRunning('nonsense', false), false);
});

test('what the page says is playing is cleaned before it is used', () => {
  assert.deepEqual(cleanNowPlaying({ title: 'Glacier', artist: 'Aurora Vale', playing: true }), { title: 'Glacier', artist: 'Aurora Vale', playing: true, hasTrack: true });
  assert.deepEqual(cleanNowPlaying({ title: '', playing: true }), { title: '', artist: '', playing: false, hasTrack: false }, 'nothing can be playing without a song');
  assert.deepEqual(cleanNowPlaying({ title: 5, artist: { x: 1 }, playing: 'yes' }), { title: '', artist: '', playing: false, hasTrack: false });
  for (const junk of [null, undefined, 'text', 42, []]) assert.deepEqual(cleanNowPlaying(junk), { title: '', artist: '', playing: false, hasTrack: false });
  const long = cleanNowPlaying({ title: 'x'.repeat(1000), artist: 'y'.repeat(1000), playing: true });
  assert.equal(long.title.length, 200);
  assert.equal(long.artist.length, 200);
});

test('the stored setting, read a moment after start-up, does not undo what the page chose in the meantime', () => {
  const setting = createCloseSetting();
  assert.equal(setting.get(), 'tray_if_playing');

  assert.equal(setting.loaded('tray'), 'tray', 'before anyone has chosen, the stored value is the one in force');
  assert.equal(setting.choose('quit'), 'quit');
  assert.equal(setting.loaded('tray'), 'quit', 'a late arrival of the stored value changes nothing once the page has chosen');
  assert.equal(setting.get(), 'quit');

  const fresh = createCloseSetting();
  fresh.choose('nonsense');
  assert.equal(fresh.get(), DEFAULT_ACTION, 'and an unknown choice is the default, which is still a choice');
  assert.equal(fresh.loaded('quit'), DEFAULT_ACTION);
});
