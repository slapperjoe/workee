const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  save: (email, password) => ipcRenderer.send('credentials-save', { email: email, password: password }),
  clear: () => ipcRenderer.send('credentials-clear'),
  close: () => ipcRenderer.send('credentials-close'),
  onInit: (cb) => ipcRenderer.on('init', (_e, d) => cb(d)),
});
