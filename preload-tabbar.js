const { contextBridge, ipcRenderer } = require('electron');

const SPK = '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3z"/><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>';
const MIC = '<svg viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/><path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg>';
const X = '<svg viewBox="0 0 24 24"><path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>';
const GEAR = '<svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94s-.02-.64-.07-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.03 7.03 0 0 0-1.62-.94l-.36-2.54a.5.5 0 0 0-.5-.42h-3.84a.5.5 0 0 0-.5.42l-.36 2.54c-.59.24-1.13.56-1.62.94l-2.39-.96a.5.5 0 0 0-.61.22L2.73 8.84a.5.5 0 0 0 .12.64l2.03 1.58c-.05.3-.07.63-.07.94s.02.64.07.94l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32c.12.22.37.31.61.22l2.39-.96c.49.38 1.03.7 1.62.94l.36 2.54c.05.24.24.42.5.42h3.84c.25 0 .45-.18.49-.42l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.24.09.49 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.02-1.58zM12 15.6a3.6 3.6 0 1 1 0-7.2 3.6 3.6 0 0 1 0 7.2z"/></svg>';
const KA = '<svg viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm0 18a8 8 0 1 1 8-8 8 8 0 0 1-8 8zm1-13H11v6h6v-2h-4z"/></svg>';
const DOTS = '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>';

function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function render(state) {
  const tabsEl = document.getElementById('tabs');
  tabsEl.innerHTML = state.tabs.map(t => {
    const name = t.title || 'VM';
    const short = name.length > 20 ? name.substring(0, 18) + '..' : name;
    const x = t.id !== 'main' ? '<span class="tx" data-id="' + esc(t.id) + '">' + X + '</span>' : '';
    return '<div class="t' + (t.active ? ' a' : '') + '" data-id="' + esc(t.id) + '">' + esc(short) + x + '</div>';
  }).join('');
  tabsEl.querySelectorAll('.t').forEach(el => {
    el.onclick = () => ipcRenderer.send('switch-tab', el.getAttribute('data-id'));
  });
  tabsEl.querySelectorAll('.tx').forEach(el => {
    el.onclick = (e) => {
      e.stopPropagation();
      ipcRenderer.send('close-tab', el.getAttribute('data-id'));
    };
  });

  const controlsEl = document.getElementById('controls');
  const spk = state.speaker;
  const mic = state.mic;
  const spkCls = spk.muted ? 'off' : (spk.audible ? 'on' : '');
  const micCls = !mic.enabled ? 'off' : (mic.active ? 'on' : '');
  const outLabel = state.devices.output || 'Speaker';
  const inLabel = state.devices.input || 'Mic';
  const ka = state.keepAlive || { on: false, duration: 30 };
  const kaCls = ka.on ? 'on' : '';
  const kaTitle = ka.on ? 'Keep session active (inactivity: ' + ka.duration + ' min)' : 'Keep session active';
  controlsEl.innerHTML =
    '<div class="ctl ' + spkCls + '" id="spk" title="Speaker: ' + esc(outLabel) + '">' + SPK +
    '<span class="dot"></span><span class="lbl">' + esc(outLabel) + '</span></div>' +
    '<div class="ctl ' + micCls + '" id="mic" title="Microphone: ' + esc(inLabel) + '">' + MIC +
    '<span class="dot"></span><span class="lbl">' + esc(inLabel) + '</span></div>' +
    '<div class="ctl ' + kaCls + '" id="ka" title="' + esc(kaTitle) + '">' + KA +
     '<span class="dot"></span><span class="lbl">Keep</span>' +
     '<span class="gear" id="kagear" title="Keep-alive inactivity duration">' + GEAR + '</span></div>' +
     '<div class="ctl" id="more" title="More (interface scale ' + esc(state.zoom) + '%)">' + DOTS + '</div>';
  document.getElementById('spk').onclick = () => ipcRenderer.send('toggle-speaker');
  document.getElementById('mic').onclick = () => ipcRenderer.send('toggle-mic');
  document.getElementById('ka').onclick = () => ipcRenderer.send('toggle-keepalive');
  document.getElementById('kagear').onclick = (e) => {
    e.stopPropagation();
    ipcRenderer.send('keepalive-gear');
  };
  document.getElementById('more').onclick = () => ipcRenderer.send('app-menu');
}

contextBridge.exposeInMainWorld('api', {
  switchTab: (id) => ipcRenderer.send('switch-tab', id),
  onState: (cb) => ipcRenderer.on('state', (_e, s) => cb(s)),
});

ipcRenderer.on('state', (_e, s) => render(s));
ipcRenderer.send('get-state');
