const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('screenshare', {
  getConfig: () => ipcRenderer.invoke('screenshare-config'),
  report: (status) => ipcRenderer.send('screenshare-status', status),
});
