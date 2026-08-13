const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('avdAPI', {
  connectVm: (vmName) => ipcRenderer.invoke('connect-vm', vmName),
});
