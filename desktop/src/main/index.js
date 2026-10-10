const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { app, BrowserWindow, dialog, globalShortcut, ipcMain, shell } = require('electron');
const { startBackend, stopBackend, DATA_DIR } = require('./backend.js');
const { createUpdater } = require('./updater.js');

// Chromium's own media-key handling would swallow the keys before the global
// shortcuts registered below ever see them.
app.commandLine.appendSwitch('disable-features', 'HardwareMediaKeyHandling,MediaSessionService');
if (process.platform === 'win32') app.setAppUserModelId('com.cblasingame.musictoolkit');

let win = null;
let backendOrigin = null;
let quitting = false;
let updater = null;

const MEDIA_KEYS = {
  MediaPlayPause: 'playpause',
  MediaNextTrack: 'next',
  MediaPreviousTrack: 'previous',
  MediaStop: 'stop',
};

const originOf = (value) => {
  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
};
const isWebUrl = (value) => ['http:', 'https:'].includes(new URL(value, 'file:///').protocol);

// Anything outside the app's own origin opens in the real browser, never in
// this window (which has the desktop bridge attached).
function openExternally(url) {
  if (typeof url === 'string' && isWebUrl(url) && originOf(url) !== backendOrigin) shell.openExternal(url);
}

// IPC is only honoured for pages served by our own backend.
function handle(channel, fn) {
  ipcMain.handle(channel, (event, ...args) => {
    const sender = event.senderFrame ? originOf(event.senderFrame.url) : null;
    return sender && sender === backendOrigin ? fn(...args) : null;
  });
}

function registerBridge() {
  handle('select-folder', async () => {
    const result = await dialog.showOpenDialog(win, { properties: ['openDirectory'] });
    return result.canceled ? null : result.filePaths[0];
  });

  handle('select-file', async (options) => {
    const filters = (Array.isArray(options && options.filters) ? options.filters : [])
      .filter((f) => f && typeof f.name === 'string' && Array.isArray(f.extensions) && f.extensions.every((e) => typeof e === 'string'))
      .map((f) => ({ name: f.name, extensions: f.extensions }));
    const result = await dialog.showOpenDialog(win, {
      properties: ['openFile'],
      filters: [...filters, { name: 'All files', extensions: ['*'] }],
    });
    return result.canceled ? null : result.filePaths[0];
  });

  handle('show-item-in-folder', (target) => {
    if (typeof target === 'string' && path.isAbsolute(target)) shell.showItemInFolder(target);
    return null;
  });

  // Folders only: the page must not be able to launch arbitrary programs.
  handle('open-path', async (target) => {
    if (typeof target !== 'string' || !path.isAbsolute(target)) return null;
    try {
      if (!fs.statSync(target).isDirectory()) return null;
    } catch {
      return null;
    }
    return (await shell.openPath(target)) || null;
  });

  handle('open-external', (url) => {
    openExternally(url);
    return null;
  });

  handle('update-status', () => updater.status());
  handle('update-check', () => updater.check());
  handle('update-install', () => updater.install());
  handle('update-auto', (on) => updater.setAuto(on));
}

function updaterLog(message) {
  try {
    fs.mkdirSync(path.join(DATA_DIR, 'logs'), { recursive: true });
    fs.appendFileSync(path.join(DATA_DIR, 'logs', 'updater.log'), `${new Date().toISOString()} ${String(message).trim()}\n`);
  } catch {
    // a log that cannot be written must never stop the app
  }
}

// The "check for updates automatically" switch lives in the app's settings (Settings page); ask the backend.
function readAutoUpdateSetting({ port, token }) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/api/settings', headers: { 'X-MTK-Token': token }, timeout: 3000 }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => {
        try {
          resolve(JSON.parse(body).app.auto_update !== false);
        } catch {
          resolve(true);
        }
      });
    });
    req.on('error', () => resolve(true));
    req.on('timeout', () => { req.destroy(); resolve(true); });
  });
}

function startUpdater(started) {
  let autoUpdater = null;
  if (app.isPackaged) {
    try {
      ({ autoUpdater } = require('electron-updater'));
      autoUpdater.logger = { info: updaterLog, warn: updaterLog, error: updaterLog, debug: () => {} };
      // Test hook (scripts/smoke_update.py): read releases from a local web server instead of GitHub.
      // Loopback addresses only, so it can never point an installed app at somebody else's server.
      const feed = process.env.MTK_UPDATE_FEED;
      if (feed && /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//.test(feed)) {
        autoUpdater.setFeedURL({ provider: 'generic', url: feed });
        updaterLog(`using the local update feed ${feed}`);
      }
    } catch (err) {
      updaterLog(`electron-updater could not be loaded: ${err && err.stack ? err.stack : err}`);
      autoUpdater = null;
    }
  }
  updater = createUpdater({
    autoUpdater,
    isPackaged: Boolean(autoUpdater),
    currentVersion: app.getVersion(),
    onStatus: (status) => {
      if (win && !win.isDestroyed()) win.webContents.send('update-status', status);
    },
    log: updaterLog,
  });
  readAutoUpdateSetting(started).then((on) => updater.setAuto(on));
}

function registerMediaKeys() {
  for (const [accelerator, key] of Object.entries(MEDIA_KEYS)) {
    // register() reports false when another app already owns the key; nothing to do then.
    globalShortcut.register(accelerator, () => {
      if (win && !win.isDestroyed()) win.webContents.send('media-key', key);
    });
  }
}

function createWindow(pageUrl) {
  win = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 720,
    minHeight: 520,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: '#0d0f14',
    title: 'Music Toolkit',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (originOf(url) === backendOrigin) return;
    event.preventDefault();
    openExternally(url);
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternally(url);
    return { action: 'deny' };
  });
  win.once('ready-to-show', () => win.show());
  win.on('closed', () => {
    win = null;
  });
  win.loadURL(pageUrl);
}

// One instance per machine — a second launch would spawn a second backend
// process fighting the first over the same database.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    let started;
    try {
      started = await startBackend({
        onExit: (why) => {
          if (quitting) return;
          dialog.showErrorBox('Music Toolkit stopped', `The background engine exited unexpectedly (${why}).\nDetails are in ~/.musictoolkit/logs/backend.log`);
          app.quit();
        },
      });
    } catch (err) {
      dialog.showErrorBox('Music Toolkit failed to start', String(err && err.message ? err.message : err));
      app.quit();
      return;
    }
    backendOrigin = `http://127.0.0.1:${started.port}`;

    startUpdater(started);
    registerBridge();
    registerMediaKeys();
    createWindow(`${backendOrigin}/?token=${encodeURIComponent(started.token)}`);
  });

  app.on('window-all-closed', () => {
    app.quit();
  });

  app.on('before-quit', () => {
    quitting = true;
    globalShortcut.unregisterAll();
    stopBackend();
  });
}
