const { contextBridge, ipcRenderer } = require('electron');

// The only thing the dashboard page gets from the desktop shell: a native
// folder picker for the Scan form. Everything else stays server-side.
contextBridge.exposeInMainWorld('mtk', {
  selectFolder: () => ipcRenderer.invoke('select-folder'),
});
