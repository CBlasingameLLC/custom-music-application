'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createUpdater, describeError, CHECK_EVERY_MS, FIRST_CHECK_MS } = require('../src/main/updater.js');

function fakeAutoUpdater(overrides = {}) {
  const updater = new EventEmitter();
  updater.checks = 0;
  updater.installs = [];
  updater.checkForUpdates = async () => {
    updater.checks += 1;
    updater.emit('checking-for-update');
    if (overrides.check) await overrides.check(updater);
  };
  updater.quitAndInstall = (...args) => updater.installs.push(args);
  return updater;
}

function fakeTimers() {
  const timers = { once: [], repeating: [], cleared: [] };
  return {
    timers,
    setTimer: (fn, ms) => { const handle = { fn, ms }; timers.once.push(handle); return handle; },
    clearTimer: (handle) => timers.cleared.push(handle),
    setRepeat: (fn, ms) => { const handle = { fn, ms }; timers.repeating.push(handle); return handle; },
    clearRepeat: (handle) => timers.cleared.push(handle),
  };
}

function build({ packaged = true, overrides, version = '0.3.0' } = {}) {
  const autoUpdater = fakeAutoUpdater(overrides);
  const seen = [];
  const clock = fakeTimers();
  const updater = createUpdater({
    autoUpdater, isPackaged: packaged, currentVersion: version, onStatus: (s) => seen.push(s), ...clock,
  });
  return { autoUpdater, updater, seen, clock };
}

test('an app that is not installed never looks for updates', async () => {
  const { autoUpdater, updater, clock } = build({ packaged: false });
  assert.equal(updater.status().state, 'disabled');
  assert.equal((await updater.check()).state, 'disabled');
  updater.setAuto(true);
  assert.equal(autoUpdater.checks, 0);
  assert.equal(clock.timers.once.length + clock.timers.repeating.length, 0);
  assert.equal(updater.install(), false);
});

test('the updater downloads quietly and installs when the app is closed', () => {
  const { autoUpdater } = build();
  assert.equal(autoUpdater.autoDownload, true);
  assert.equal(autoUpdater.autoInstallOnAppQuit, true);
  assert.equal(autoUpdater.allowPrerelease, false);
  assert.equal(autoUpdater.allowDowngrade, false);
});

test('a newer version goes from checking to downloading to ready, and installs on request', async () => {
  const { autoUpdater, updater, seen } = build({
    overrides: { check: async (u) => { u.emit('update-available', { version: '0.3.1' }); u.emit('download-progress', { percent: 41.6 }); u.emit('update-downloaded', { version: '0.3.1' }); } },
  });
  assert.equal(updater.install(), false, 'nothing to install yet');

  const after = await updater.check();

  assert.deepEqual(seen.map((s) => s.state), ['checking', 'downloading', 'downloading', 'ready']);
  assert.equal(seen[2].percent, 42);
  assert.equal(after.state, 'ready');
  assert.equal(after.version, '0.3.1');
  assert.equal(after.current, '0.3.0');
  assert.equal(updater.install(), true);
  assert.deepEqual(autoUpdater.installs, [[true, true]]);
});

test('no newer version is reported as up to date, with the time of the check', async () => {
  const { updater } = build({ overrides: { check: async (u) => u.emit('update-not-available', { version: '0.3.0' }) } });
  const before = Date.now();
  const status = await updater.check();
  assert.equal(status.state, 'up-to-date');
  assert.ok(status.checkedAt >= before);
  assert.equal(status.version, null);
});

test('a check is not repeated while one is running, downloading, or waiting to be installed', async () => {
  const { autoUpdater, updater } = build({ overrides: { check: async (u) => u.emit('update-downloaded', { version: '0.3.1' }) } });
  await updater.check();
  await updater.check();
  await updater.check();
  assert.equal(autoUpdater.checks, 1);
});

test('failures become a sentence, and the next check is allowed', async () => {
  let fail = true;
  const { autoUpdater, updater } = build({
    overrides: { check: async (u) => { if (fail) { const error = Object.assign(new Error('getaddrinfo ENOTFOUND github.com'), { code: 'ENOTFOUND' }); u.emit('error', error); throw error; } u.emit('update-not-available', {}); } },
  });
  const failed = await updater.check();
  assert.equal(failed.state, 'error');
  assert.match(failed.error, /offline/i);

  fail = false;
  assert.equal((await updater.check()).state, 'up-to-date');
  assert.equal(autoUpdater.checks, 2);
});

test('a check that throws before any event still ends in an error state', async () => {
  const { updater } = build({ overrides: { check: async () => { throw new Error('boom\nstack line'); } } });
  const status = await updater.check();
  assert.equal(status.state, 'error');
  assert.equal(status.error, 'boom');
});

test('error text is made friendly', () => {
  assert.match(describeError(Object.assign(new Error('x'), { code: 'ETIMEDOUT' })), /offline/i);
  assert.match(describeError(new Error('net::ERR_INTERNET_DISCONNECTED')), /offline/i);
  assert.match(describeError(new Error('Cannot find latest.yml in the latest release artifacts (https://example/latest.yml): HttpError: 404')), /no update information/i);
  assert.equal(describeError(new Error('first line\nsecond line')), 'first line');
  assert.equal(describeError(null), 'unknown error');
  assert.equal(describeError(new Error('x'.repeat(500))).length, 200);
});

test('automatic checks run shortly after start and then every few hours, and can be switched off', () => {
  const { updater, clock, autoUpdater } = build();
  updater.setAuto(true);
  assert.equal(clock.timers.once[0].ms, FIRST_CHECK_MS);
  assert.equal(clock.timers.repeating[0].ms, CHECK_EVERY_MS);
  clock.timers.once[0].fn();
  assert.equal(autoUpdater.checks, 1);

  updater.setAuto(false);
  assert.equal(updater.isAuto(), false);
  assert.equal(clock.timers.cleared.length, 2, 'both timers were cancelled');
  assert.equal(clock.timers.once.length, 1, 'nothing new was scheduled');

  updater.setAuto(true);
  assert.equal(clock.timers.once.length, 2);
});

test('switching automatic checks off still allows a manual check', async () => {
  const { updater, autoUpdater } = build({ overrides: { check: async (u) => u.emit('update-not-available', {}) } });
  updater.setAuto(false);
  await updater.check();
  assert.equal(autoUpdater.checks, 1);
});
