'use strict';

// Keeps the installed app current. It asks GitHub Releases (through electron-updater) whether a newer
// version exists, downloads it quietly in the background, and installs it when the app is closed, or
// right away when the person presses "Restart and install". Nothing here is Electron specific: the
// updater, the timers and the status callback are passed in, so the logic runs under plain `node --test`.

const CHECK_EVERY_MS = 6 * 60 * 60 * 1000;
const FIRST_CHECK_MS = 15 * 1000;
const BUSY = ['checking', 'downloading', 'ready'];
const OFFLINE_CODES = ['ENOTFOUND', 'ECONNREFUSED', 'ETIMEDOUT', 'ECONNRESET', 'EAI_AGAIN', 'ENETUNREACH'];

/** A sentence for the Settings page instead of a stack trace. */
function describeError(error) {
  const text = String((error && error.message) || error || 'unknown error');
  const code = error && error.code;
  if (OFFLINE_CODES.includes(code) || /net::ERR_(INTERNET_DISCONNECTED|NAME_NOT_RESOLVED|CONNECTION|TIMED_OUT)/.test(text)) {
    return "Couldn't reach GitHub to look for updates. Are you offline?";
  }
  if (/latest\.yml/i.test(text) && /(404|not found|cannot find)/i.test(text)) {
    return 'The newest release has no update information yet.';
  }
  return text.split('\n')[0].slice(0, 200);
}

function createUpdater({
  autoUpdater,
  isPackaged,
  currentVersion,
  onStatus = () => {},
  log = () => {},
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  setRepeat = setInterval,
  clearRepeat = clearInterval,
}) {
  const status = {
    state: isPackaged ? 'idle' : 'disabled', // idle | checking | up-to-date | downloading | ready | error | disabled
    current: currentVersion,
    version: null, // the version being downloaded or waiting to be installed
    percent: 0,
    error: null,
    checkedAt: null,
  };
  let auto = true;
  let first = null;
  let repeat = null;

  const snapshot = () => ({ ...status });
  function publish(patch) {
    Object.assign(status, patch);
    onStatus(snapshot());
  }

  if (isPackaged) {
    autoUpdater.autoDownload = true; // the installer is fetched in the background...
    autoUpdater.autoInstallOnAppQuit = true; // ...and runs, silently, when the app is closed
    autoUpdater.allowPrerelease = false;
    autoUpdater.allowDowngrade = false;
    autoUpdater.on('checking-for-update', () => publish({ state: 'checking', error: null }));
    autoUpdater.on('update-available', (info) => publish({ state: 'downloading', version: info.version, percent: 0 }));
    autoUpdater.on('update-not-available', () => publish({ state: 'up-to-date', version: null, percent: 0, error: null, checkedAt: Date.now() }));
    autoUpdater.on('download-progress', (progress) => publish({ state: 'downloading', percent: Math.max(0, Math.min(100, Math.round(progress.percent || 0))) }));
    autoUpdater.on('update-downloaded', (info) => publish({ state: 'ready', version: info.version, percent: 100, error: null }));
    autoUpdater.on('error', (error) => {
      log(`update error: ${error && error.stack ? error.stack : error}`);
      publish({ state: 'error', error: describeError(error), checkedAt: Date.now() });
    });
  }

  async function check() {
    if (!isPackaged || BUSY.includes(status.state)) return snapshot();
    try {
      await autoUpdater.checkForUpdates();
    } catch (error) {
      // the 'error' event usually reported it already; this covers a failure before any event
      if (status.state !== 'error') publish({ state: 'error', error: describeError(error), checkedAt: Date.now() });
    }
    return snapshot();
  }

  function install() {
    if (status.state !== 'ready') return false;
    log(`installing ${status.version} on request`);
    autoUpdater.quitAndInstall(true, true); // silent, and start the app again afterwards
    return true;
  }

  function schedule() {
    if (first) clearTimer(first);
    if (repeat) clearRepeat(repeat);
    first = repeat = null;
    if (!isPackaged || !auto) return;
    first = setTimer(() => { check(); }, FIRST_CHECK_MS);
    repeat = setRepeat(() => { check(); }, CHECK_EVERY_MS);
    for (const handle of [first, repeat]) if (handle && typeof handle.unref === 'function') handle.unref();
  }

  function setAuto(on) {
    auto = Boolean(on);
    schedule();
    return snapshot();
  }

  return { status: snapshot, check, install, setAuto, isAuto: () => auto };
}

module.exports = { createUpdater, describeError, CHECK_EVERY_MS, FIRST_CHECK_MS };
