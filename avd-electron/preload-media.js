const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('__workee', {
  report: function (state) { ipcRenderer.send('media-state', state); },
  devices: function (names) { ipcRenderer.send('media-devices', names); },
  keepAliveInput: function () { ipcRenderer.send('keepalive-input'); },
});
