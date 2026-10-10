const { contextBridge, ipcRenderer } = require('electron');

// What the Music Toolkit page gets from the desktop shell: native pickers,
// "show in folder", links that open in the real browser, the keyboard's
// media keys, and app updates. Everything else stays in the Python backend.
// The main process ignores these calls from any page that is not the backend's own.
const mediaKeyListeners = new Set();
ipcRenderer.on('media-key', (_event, key) => {
  for (const listener of mediaKeyListeners) listener(key);
});
const updateListeners = new Set();
ipcRenderer.on('update-status', (_event, status) => {
  for (const listener of updateListeners) listener(status);
});

contextBridge.exposeInMainWorld('mtk', {
  selectFolder: () => ipcRenderer.invoke('select-folder'),
  selectFile: (options) => ipcRenderer.invoke('select-file', options),
  showItemInFolder: (target) => ipcRenderer.invoke('show-item-in-folder', target),
  openPath: (target) => ipcRenderer.invoke('open-path', target),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  // Which of play/pause, next, previous and stop this app managed to claim: { playpause: true, ... }.
  mediaKeys: () => ipcRenderer.invoke('media-keys'),
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
