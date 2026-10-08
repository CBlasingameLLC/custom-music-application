const path = require('node:path');
const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const { startBackend, stopBackend } = require('./backend.js');

let win = null;
let backendOrigin = null;

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
    let port;
    try {
      port = await startBackend();
    } catch (err) {
      dialog.showErrorBox('Music Toolkit failed to start', String(err && err.message ? err.message : err));
      app.quit();
      return;
    }
    backendOrigin = `http://127.0.0.1:${port}`;

    ipcMain.handle('select-folder', async (event) => {
      // Only the backend's own pages may ask for the picker.
      if (!event.senderFrame || !event.senderFrame.url.startsWith(backendOrigin)) return null;
      const result = await dialog.showOpenDialog(win, { properties: ['openDirectory'] });
      return result.canceled ? null : result.filePaths[0];
    });

    win = new BrowserWindow({
      width: 1280,
      height: 860,
      show: false,
      autoHideMenuBar: true,
      webPreferences: {
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    win.webContents.on('will-navigate', (event, url) => {
      if (!url.startsWith(backendOrigin)) event.preventDefault();
    });
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.once('ready-to-show', () => win.show());
    win.loadURL(`${backendOrigin}/`);

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) win.loadURL(`${backendOrigin}/`);
    });
  });

  app.on('window-all-closed', () => {
    stopBackend();
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => {
    stopBackend();
  });
}
