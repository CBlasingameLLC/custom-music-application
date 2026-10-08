const { contextBridge, ipcRenderer } = require('electron');

// What the Music Toolkit page gets from the desktop shell: native pickers,
// "show in folder", links that open in the real browser, and the keyboard's
// media keys. Everything else stays in the Python backend. The main process
// ignores these calls from any page that is not the backend's own.
const mediaKeyListeners = new Set();
ipcRenderer.on('media-key', (_event, key) => {
  for (const listener of mediaKeyListeners) listener(key);
});

contextBridge.exposeInMainWorld('mtk', {
  selectFolder: () => ipcRenderer.invoke('select-folder'),
  selectFile: (options) => ipcRenderer.invoke('select-file', options),
  showItemInFolder: (target) => ipcRenderer.invoke('show-item-in-folder', target),
  openPath: (target) => ipcRenderer.invoke('open-path', target),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  // Returns a function that stops listening.
  onMediaKey: (listener) => {
    mediaKeyListeners.add(listener);
    return () => mediaKeyListeners.delete(listener);
  },
});
