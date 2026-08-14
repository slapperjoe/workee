const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('__workee', {
  report: (state) => ipcRenderer.send('media-state', state),
  devices: (list) => ipcRenderer.send('media-devices', list),
});
