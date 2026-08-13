const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('api', {
  switchTab: (id) => ipcRenderer.send('switch-tab', id),
});
