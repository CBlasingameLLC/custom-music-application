const { app, BrowserWindow, dialog } = require('electron');
const { startBackend, stopBackend } = require('./backend.js');

let win = null;

// One instance per machine — a second launch would spawn a second backend
// process fighting the first over the same port.
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

    win = new BrowserWindow({
      width: 1280,
      height: 860,
      show: false,
      autoHideMenuBar: true,
    });
    win.once('ready-to-show', () => win.show());
    win.loadURL(`http://127.0.0.1:${port}/`);

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) win.loadURL(`http://127.0.0.1:${port}/`);
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
