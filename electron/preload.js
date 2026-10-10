const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),
  onServerStatus: (cb) => ipcRenderer.on('server-status', (_, data) => cb(data)),
  onModelInfo: (cb) => ipcRenderer.on('model-info', (_, data) => cb(data)),
});
