const { contextBridge, ipcRenderer, webUtils } = require('electron');

// What the Music Toolkit page gets from the desktop shell: native pickers,
// "show in folder", links that open in the real browser, the keyboard's
// media keys, the tray, songs opened with the app, and app updates. Everything
// else stays in the Python backend.
// The main process ignores these calls from any page that is not the backend's own.
const mediaKeyListeners = new Set();
ipcRenderer.on('media-key', (_event, key) => {
  for (const listener of mediaKeyListeners) listener(key);
});
const updateListeners = new Set();
ipcRenderer.on('update-status', (_event, status) => {
  for (const listener of updateListeners) listener(status);
});
const openFileListeners = new Set();
ipcRenderer.on('open-files', (_event, files) => {
  for (const listener of openFileListeners) listener(files);
});

contextBridge.exposeInMainWorld('mtk', {
  selectFolder: () => ipcRenderer.invoke('select-folder'),
  selectFile: (options) => ipcRenderer.invoke('select-file', options),
  showItemInFolder: (target) => ipcRenderer.invoke('show-item-in-folder', target),
  openPath: (target) => ipcRenderer.invoke('open-path', target),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  // Which of play/pause, next, previous and stop this app managed to claim: { playpause: true, ... }.
  mediaKeys: () => ipcRenderer.invoke('media-keys'),
  // Tell the shell what is playing ({ title, artist, playing }): the tray's tooltip and menu, and whether closing the
  // window should keep the music going.
  setNowPlaying: (info) => ipcRenderer.invoke('now-playing', info),
  // What the close button does: 'quit', 'tray_if_playing' or 'tray' (the setting lives in the backend; this keeps the shell in step).
  setCloseAction: (action) => ipcRenderer.invoke('close-action', action),
  // Songs and folders the app was opened with (paths), collected once at start-up; later ones arrive through onOpenFiles.
  takeOpenFiles: () => ipcRenderer.invoke('take-open-files'),
  onOpenFiles: (listener) => {
    openFileListeners.add(listener);
    return () => openFileListeners.delete(listener);
  },
  // The path of a file or folder dropped on the window (null if it has none).
  pathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file) || null;
    } catch {
      return null;
    }
  },
  // { tray: whether a tray icon exists, closeAction, hidden }: for Settings > Diagnostics.
  desktopInfo: () => ipcRenderer.invoke('desktop-info'),
  quit: () => ipcRenderer.invoke('quit'),
  // Returns a function that stops listening.
  onMediaKey: (listener) => {
    mediaKeyListeners.add(listener);
    return () => mediaKeyListeners.delete(listener);
  },
  update: {
    status: () => ipcRenderer.invoke('update-status'),
    check: () => ipcRenderer.invoke('update-check'),
    install: () => ipcRenderer.invoke('update-install'),
    setAuto: (on) => ipcRenderer.invoke('update-auto', Boolean(on)),
    // Returns a function that stops listening.
    onStatus: (listener) => {
      updateListeners.add(listener);
      return () => updateListeners.delete(listener);
    },
  },
});
