const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { app, BrowserWindow, Menu, Tray, dialog, globalShortcut, ipcMain, nativeImage, screen, shell } = require('electron');
const { startBackend, stopBackend, DATA_DIR } = require('./backend.js');
const { createUpdater } = require('./updater.js');
const { createCloseSetting, shouldKeepRunning, cleanNowPlaying } = require('./close-action.js');
const { filesFromArgv } = require('./launch.js');
const { tooltipText, trayMenuTemplate } = require('./tray-menu.js');
const { MIN_SIZE, createSaver, fitToDisplays, loadState } = require('./window-state.js');

// Chromium's own media-key handling would swallow the keys before the global
// shortcuts registered below ever see them.
app.commandLine.appendSwitch('disable-features', 'HardwareMediaKeyHandling,MediaSessionService');
if (process.platform === 'win32') app.setAppUserModelId('com.cblasingame.musictoolkit');

let win = null;
let tray = null;
let backendOrigin = null;
let quitting = false;
let updater = null;
let windowSaver = null;
const closeSetting = createCloseSetting(); // what the close button does; the Settings page keeps this in step (and the backend remembers it)
let nowPlaying = cleanNowPlaying(null); // what the page last said is playing: the tray's tooltip, and whether closing keeps it going
let trayNoticeShown = false; // the "still playing in the tray" notice is shown once, ever
let pendingFiles = []; // songs handed to the app that the page has not collected yet
let pageListening = false; // the page asked for them, so it also hears about later ones

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

  handle('media-keys', () => ({ ...mediaKeyState }));

  // What is playing, for the tray tooltip and menu.
  handle('now-playing', (info) => {
    nowPlaying = cleanNowPlaying(info);
    updateTray();
    return null;
  });
  handle('close-action', (value) => {
    const chosen = closeSetting.choose(value);
    desktopLog(`the close button's choice is now ${chosen}`);
    return chosen;
  });
  // Songs the person opened with the app (Explorer's Open with, a file dragged onto the program): the page collects the
  // ones that arrived before it was ready, and from then on is sent the rest as they come.
  handle('take-open-files', () => {
    pageListening = true;
    return pendingFiles.splice(0);
  });
  handle('quit', () => {
    desktopLog('the page asked the app to quit');
    setImmediate(() => app.quit());
    return null;
  });
  handle('desktop-info', () => ({ tray: Boolean(tray), closeAction: closeSetting.get(), hidden: Boolean(win && !win.isVisible()) }));

  handle('update-status', () => updater.status());
  handle('update-check', () => updater.check());
  handle('update-install', () => updater.install());
  handle('update-auto', (on) => updater.setAuto(on));
}

function writeLog(file, message) {
  try {
    fs.mkdirSync(path.join(DATA_DIR, 'logs'), { recursive: true });
    fs.appendFileSync(path.join(DATA_DIR, 'logs', file), `${new Date().toISOString()} ${String(message).trim()}\n`);
  } catch {
    // a log that cannot be written must never stop the app
  }
}
const updaterLog = (message) => writeLog('updater.log', message);
const desktopLog = (message) => writeLog('desktop.log', message);

// Settings the main process needs before the page says anything: the automatic-update switch and the close button's
// behaviour live in the backend's config (Settings page); ask the backend. Resolves to null if it cannot be read.
function fetchSettings({ port, token }) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/api/settings', headers: { 'X-MTK-Token': token }, timeout: 3000 }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch {
          resolve(null);
        }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

function startUpdater(settings) {
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
  settings.then((found) => updater.setAuto(!found || !found.app || found.app.auto_update !== false));
}

// Which media keys this app actually owns, for Settings > Diagnostics. register() reports false (or
// isRegistered() stays false) when another app already holds a key; there is nothing to do about that
// except tell the person.
const mediaKeyState = { playpause: false, next: false, previous: false, stop: false };

function registerMediaKeys() {
  for (const [accelerator, key] of Object.entries(MEDIA_KEYS)) {
    const registered = globalShortcut.register(accelerator, () => {
      if (win && !win.isDestroyed()) win.webContents.send('media-key', key);
    });
    mediaKeyState[key] = Boolean(registered) || globalShortcut.isRegistered(accelerator);
  }
}

// ---- the tray -----------------------------------------------------------------------------------------------------

function showWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function toggleWindow() {
  if (win && !win.isDestroyed() && win.isVisible() && !win.isMinimized()) win.hide();
  else showWindow();
}

// Play/pause, next and previous reach the page the way the keyboard's media keys do.
const sendKey = (key) => {
  if (win && !win.isDestroyed()) win.webContents.send('media-key', key);
};

const trayActions = {
  toggleWindow,
  playPause: () => sendKey('playpause'),
  next: () => sendKey('next'),
  previous: () => sendKey('previous'),
  quit: () => app.quit(),
};

function updateTray() {
  if (!tray) return;
  try {
    const visible = Boolean(win && !win.isDestroyed() && win.isVisible());
    tray.setContextMenu(Menu.buildFromTemplate(trayMenuTemplate({ windowVisible: visible, playing: nowPlaying.playing, hasTrack: nowPlaying.hasTrack }, trayActions)));
    tray.setToolTip(tooltipText(nowPlaying));
  } catch (err) {
    desktopLog(`the tray could not be updated: ${err && err.message}`);
  }
}

// A computer with no notification area (or a build with no icon) simply has no tray: the close button then always quits,
// so the window can never be hidden with no way to bring it back.
function createTray() {
  try {
    const icon = nativeImage.createFromPath(path.join(__dirname, 'tray.png')); // tray@2x.png is picked up for sharp screens
    if (icon.isEmpty()) throw new Error('the tray icon is missing');
    tray = new Tray(icon);
    tray.on('click', toggleWindow);
    tray.on('double-click', showWindow);
    updateTray();
  } catch (err) {
    desktopLog(`no tray: ${err && err.message}`);
    tray = null;
  }
}

// Told once, the first time closing the window leaves the app running.
function announceTray() {
  if (trayNoticeShown || !tray) return;
  trayNoticeShown = true;
  if (windowSaver) windowSaver.flush();
  try {
    if (typeof tray.displayBalloon === 'function') {
      tray.displayBalloon({ title: 'Music Toolkit is still running', content: 'It keeps playing from the tray. Right-click the icon to quit.', iconType: 'info' });
    }
  } catch {
    // a notice that cannot be shown is not worth a crash
  }
}

// ---- songs opened with the app ------------------------------------------------------------------------------------

function handOverFiles(files) {
  if (!files.length) return;
  pendingFiles.push(...files);
  if (pageListening && win && !win.isDestroyed()) win.webContents.send('open-files', pendingFiles.splice(0));
}

// ---- the window ---------------------------------------------------------------------------------------------------

function createWindow(pageUrl) {
  const stateFile = path.join(app.getPath('userData'), 'window-state.json');
  const saved = loadState(stateFile);
  trayNoticeShown = Boolean(saved.trayNoticeShown);
  win = new BrowserWindow({
    ...fitToDisplays(saved, screen.getAllDisplays().map((display) => display.workArea)),
    minWidth: MIN_SIZE.width,
    minHeight: MIN_SIZE.height,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: '#0d0f14',
    title: 'Music Toolkit',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // The gapless and crossfade clock runs in the page; a window hidden in the tray must not slow it down.
      backgroundThrottling: false,
    },
  });
  if (saved.maximized) win.maximize();
  windowSaver = createSaver({
    file: stateFile,
    read: () => (win && !win.isDestroyed() ? { ...win.getNormalBounds(), maximized: win.isMaximized(), trayNoticeShown } : null),
  });
  win.on('resize', () => windowSaver.schedule());
  win.on('move', () => windowSaver.schedule());
  win.on('show', updateTray);
  win.on('hide', updateTray);
  win.on('close', (event) => {
    windowSaver.flush();
    desktopLog(`window closing: quitting=${quitting} tray=${Boolean(tray)} close=${closeSetting.get()} playing=${nowPlaying.playing}`);
    // Closing while music plays (or always, if chosen) leaves the app running in the tray; the tray menu or Settings quits.
    if (quitting || !tray || !shouldKeepRunning(closeSetting.get(), nowPlaying.playing)) return;
    event.preventDefault();
    win.hide();
    announceTray();
  });
  // A page that loads again (not one that only moves to another route inside itself) has to ask for its songs again.
  win.webContents.on('did-navigate', () => {
    pageListening = false;
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
  win.once('ready-to-show', () => {
    win.show();
    desktopLog(`the window is ready and is shown (visible=${win.isVisible()}, bounds=${JSON.stringify(win.getBounds())})`);
  });
  win.on('unresponsive', () => desktopLog('the window is not responding'));
  win.on('responsive', () => desktopLog('the window responds again'));
  win.on('closed', () => {
    desktopLog('the window is closed');
    win = null;
  });
  win.loadURL(pageUrl);
}

// One instance per machine — a second launch would spawn a second backend
// process fighting the first over the same database. A second launch that names songs
// ("Open with") hands them to the first, which comes to the front.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv, workingDirectory) => {
    handOverFiles(filesFromArgv(argv, { defaultApp: process.defaultApp, cwd: workingDirectory }));
    showWindow();
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

    const settings = fetchSettings(started);
    settings.then((found) => {
      if (found && found.app) closeSetting.loaded(found.app.close_action);
    });
    startUpdater(settings);
    registerBridge();
    registerMediaKeys();
    const given = filesFromArgv(process.argv, { defaultApp: process.defaultApp });
    desktopLog(`started: version ${app.getVersion()}, packaged=${app.isPackaged}, ${given.length} song(s) named on the command line`);
    handOverFiles(given);
    createTray();
    createWindow(`${backendOrigin}/?token=${encodeURIComponent(started.token)}`);
  });

  app.on('window-all-closed', () => {
    app.quit();
  });

  // A page or helper process that dies leaves its reason here, where a person reporting "it froze" can find it.
  app.on('render-process-gone', (_event, _contents, details) => desktopLog(`a page process is gone: ${details.reason} (exit code ${details.exitCode})`));
  app.on('child-process-gone', (_event, details) => desktopLog(`a ${details.type} process is gone: ${details.reason} (exit code ${details.exitCode})`));

  app.on('before-quit', () => {
    desktopLog('quitting');
    quitting = true;
    globalShortcut.unregisterAll();
    if (tray) {
      tray.destroy(); // otherwise Windows leaves a dead icon in the notification area until the mouse passes over it
      tray = null;
    }
    stopBackend();
    desktopLog('the backend is stopped');
  });

  app.on('will-quit', () => desktopLog('every window is closed; the app is exiting'));
}
