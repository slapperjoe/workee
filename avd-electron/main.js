if (process.platform === 'linux' && !process.env.GDK_BACKEND) {
  process.env.GDK_BACKEND = 'x11';
}

const { app, BrowserWindow, BrowserView, ipcMain, session, desktopCapturer, Menu, screen, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { execSync } = require('child_process');
const store = require('./store');
const monitor = require('./monitor');
monitor.init();

const AVD_URL = 'https://windows.cloud.microsoft/#/devices';

let mainWindow = null;
let tabBarView = null;
let tabs = [];
let activeTabId = 'main';
let tabIdCounter = 0;

let screenshareWindow = null;
let screensharing = false;
let screenshareConnected = false;
let screenCam = false;
let screenCamSettings = { width: 1280, height: 720, fps: 30, smooth: 0 };
let captureReady = false;
let keepAliveDuration = 30;
let kaIntent = false;
let blockFullscreen = false;
let zoomPercent = 100;
const signalingUrl = process.env.SCREENSHARE_SIGNALING_URL || 'ws://localhost:8080';

let mediaHook = '';
try {
  mediaHook = fs.readFileSync(path.join(__dirname, 'media-hook.js'), 'utf8');
} catch (e) {}

const tabBarContent = `<!DOCTYPE html><html><head><style>
  html,body{margin:0;padding:0;background:#161b22;overflow:hidden;height:36px;font-family:system-ui,sans-serif}
  .bar{display:flex;align-items:center;height:36px;width:100%}
  .tabs{display:flex;flex:1;overflow:hidden;height:36px}
  .t{display:inline-flex;align-items:center;padding:0 14px;font-size:12px;cursor:pointer;border-right:1px solid #30363d;white-space:nowrap;flex-shrink:0;color:#8b949e;height:36px}
  .t:hover{background:#21262d}
  .t.a{background:#0d1117;color:#c9d1d9}
  .t .tx{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;margin-left:6px;border-radius:3px;flex-shrink:0}
  .t .tx svg{width:10px;height:10px;fill:#8b949e}
  .t .tx:hover{background:#30363d}
  .t .tx:hover svg{fill:#c9d1d9}
  .controls{display:flex;align-items:center;gap:6px;padding:0 10px;border-left:1px solid #30363d;height:36px;flex-shrink:0}
  .ctl{display:flex;align-items:center;gap:5px;cursor:pointer;padding:0 6px;height:36px;color:#8b949e;user-select:none;position:relative}
  .ctl:hover{background:#21262d}
  .ctl svg{width:15px;height:15px;fill:#8b949e}
  .ctl .dot{width:7px;height:7px;border-radius:50%;background:#30363d}
  .ctl.on .dot{background:#3fb950;box-shadow:0 0 4px #3fb950}
  .ctl.obs .dot{background:#58a6ff;box-shadow:0 0 4px #58a6ff}
  .ctl.off{color:#f85149}
  .ctl.off svg{fill:#f85149}
  .ctl.off::after{content:'';position:absolute;left:6px;top:17px;width:15px;height:2px;background:#f85149;border-radius:1px;transform:rotate(-24deg);pointer-events:none}
  .ctl.warn .dot{background:#d29922}
  .ctl .gear{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;border-radius:3px;flex-shrink:0}
  .ctl .gear svg{width:12px;height:12px;fill:#8b949e}
  .ctl .gear:hover{background:#30363d}
  .ctl .gear:hover svg{fill:#c9d1d9}
  .ctl .lbl{font-size:10px;color:#6e7681;max-width:88px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  </style></head><body>
  <div class="bar">
    <div class="tabs" id="tabs"></div>
    <div class="controls" id="controls"></div>
  </div>
  </body></html>`;

function activeTab() {
  for (let i = 0; i < tabs.length; i++) if (tabs[i].id === activeTabId) return tabs[i];
  return null;
}

function tabForWebContents(wc) {
  for (let i = 0; i < tabs.length; i++) if (tabs[i].view && tabs[i].view.webContents === wc) return tabs[i];
  return null;
}

function layout() {
  if (!mainWindow) return;
  const [w, h] = mainWindow.getSize();
  const th = 36;
  if (tabBarView) tabBarView.setBounds({ x: 0, y: 0, width: w, height: th });
  tabs.forEach(t => {
    if (t.id === activeTabId) {
      t.view.setBounds({ x: 0, y: th, width: w, height: h - th });
    } else {
      t.view.setBounds({ x: 0, y: th, width: 0, height: 0 });
    }
  });
}

function pushState() {
  if (!tabBarView) return;
  const t = activeTab();
  const state = {
    tabs: tabs.map(x => ({ id: x.id, title: x.title, active: x.id === activeTabId })),
    speaker: { muted: t ? t.muted : false, audible: t ? t.audible : false },
    mic: { enabled: t ? t.micEnabled : true, active: t ? t.micActive : false },
    videoSource: t ? t.videoSource : null,
    devices: { input: t ? t.inputLabel : '', output: t ? t.outputLabel : '' },
    screenshare: { on: screensharing, connected: screenshareConnected },
    screencam: screenCam,
    captureReady: captureReady,
    keepAlive: { on: kaIntent, duration: keepAliveDuration },
    blockFullscreen: blockFullscreen,
    zoom: zoomPercent,
  };
  tabBarView.webContents.send('state', state);
}

function switchTab(id) {
  activeTabId = id;
  layout();
  pushState();
}

function createTab(id, url, title) {
  const tab = {
    id: id,
    title: title || 'VM',
    audible: false,
    muted: false,
    micEnabled: true,
    micActive: false,
    videoActive: false,
    videoSource: null,
    inputLabel: '',
    outputLabel: '',
    view: null,
  };
  const view = new BrowserView({
    webPreferences: {
      preload: path.join(__dirname, 'preload-media.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  tab.view = view;
  if (kaIntent && id !== 'main') { tab.kaActive = true; tab.kaStart = Date.now(); }
  tabs.push(tab);

  const wc = view.webContents;
  try { wc.setZoomFactor(zoomPercent / 100); } catch (e) {}
  wc.on('page-title-updated', (e, t) => {
    if (t) { tab.title = t; pushState(); }
  });
  wc.on('audio-state-changed', (e) => {
    tab.audible = !!(e && e.audible);
    pushState();
  });
  const inject = () => {
    try { wc.setZoomFactor(zoomPercent / 100); } catch (e) {}
    if (!mediaHook) return;
    const opts = JSON.stringify(screenCamSettings);
    wc.executeJavaScript(mediaHook).then(() => {
      return wc.executeJavaScript(
        'window.__workeeSetScreenCam && window.__workeeSetScreenCam(' + (screenCam ? 'true' : 'false') + ', ' + opts + ');' +
        'window.__workeeSetBlockFullscreen && window.__workeeSetBlockFullscreen(' + (blockFullscreen ? 'true' : 'false') + ')'
      );
    }).catch(() => {});
  };
  wc.on('dom-ready', inject);
  wc.on('did-finish-load', inject);

  if (mainWindow) mainWindow.addBrowserView(view);
  wc.loadURL(url);
  return tab;
}

function addTab(url, title) {
  const tab = createTab('t' + (++tabIdCounter), url, title);
  switchTab(tab.id);
}

function closeTab(id) {
  const idx = tabs.findIndex(t => t.id === id);
  if (idx < 0) return;
  const tab = tabs[idx];
  tabs.splice(idx, 1);
  try { mainWindow.removeBrowserView(tab.view); } catch (e) {}
  try { tab.view.webContents.destroy(); } catch (e) {}
  if (activeTabId === id) {
    activeTabId = tabs.length ? tabs[Math.max(0, idx - 1)].id : 'main';
  }
  layout();
  pushState();
}

function startScreenShare() {
  if (screenshareWindow) return;
  screensharing = true;
  screenshareConnected = false;
  screenshareWindow = new BrowserWindow({
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload-screenshare.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  screenshareWindow.loadFile(path.join(__dirname, 'screenshare-capture.html'));
  screenshareWindow.on('closed', () => {
    screenshareWindow = null;
    screensharing = false;
    screenshareConnected = false;
    pushState();
  });
  pushState();
}

function stopScreenShare() {
  if (screenshareWindow) screenshareWindow.close();
}

function fmtRemaining(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const min = Math.floor(total / 60);
  const sec = total % 60;
  return min > 0 ? (min + 'm ' + sec + 's') : (sec + 's');
}

function sendHarmlessKey(wc) {
  if (!wc || wc.isDestroyed()) return;
  const tab = tabForWebContents(wc);
  const name = tab ? (tab.title || tab.id) : 'tab';
  const remaining = tab ? (keepAliveDuration * 60 * 1000 - (Date.now() - (tab.kaStart || Date.now()))) : 0;
  try {
    if (typeof wc.executeJavaScript === 'function') {
      const p = wc.executeJavaScript('window.__workeeMarkKA && window.__workeeMarkKA()');
      if (p && typeof p.catch === 'function') p.catch(function () {});
    }
    if (typeof wc.sendInputEvent === 'function') {
      const okDown = safeInput(wc, { type: 'keyDown', keyCode: 'Control', code: 'ControlLeft', key: 'Control', modifiers: [] });
      const okUp = safeInput(wc, { type: 'keyUp', keyCode: 'Control', code: 'ControlLeft', key: 'Control', modifiers: [] });
      console.log('[keep-alive] nudge (Control) sent to ' + name + ' at ' + new Date().toLocaleTimeString() + ' (' + fmtRemaining(remaining) + ' left until wake-up ends)');
    } else {
      console.warn('[keep-alive] sendInputEvent unavailable for ' + name);
    }
  } catch (e) {
    console.warn('[keep-alive] nudge error for ' + name + ':\n' + ((e && e.stack) || (e && e.message) || e));
  }
}

function safeInput(wc, evt) {
  try {
    const r = wc.sendInputEvent(evt);
    if (r && typeof r.catch === 'function') {
      r.catch(function (e) { console.warn('[keep-alive] ' + evt.type + ' failed:\n' + ((e && e.stack) || (e && e.message) || e)); });
      return true;
    }
    return r;
  } catch (e) {
    console.warn('[keep-alive] ' + evt.type + ' threw:\n' + ((e && e.stack) || (e && e.message) || e));
    return false;
  }
}

function applyScreenCam() {
  const opts = JSON.stringify(screenCamSettings);
  tabs.forEach(t => {
    t.view.webContents.executeJavaScript(
      'window.__workeeSetScreenCam && window.__workeeSetScreenCam(' + (screenCam ? 'true' : 'false') + ', ' + opts + ')'
    ).catch(() => {});
  });
}

function applyBlockFullscreen() {
  tabs.forEach(t => {
    t.view.webContents.executeJavaScript(
      'window.__workeeSetBlockFullscreen && window.__workeeSetBlockFullscreen(' + (blockFullscreen ? 'true' : 'false') + ')'
    ).catch(() => {});
  });
}

function toggleBlockFullscreen() {
  blockFullscreen = !blockFullscreen;
  store.set('blockFullscreen', blockFullscreen);
  applyBlockFullscreen();
  pushState();
}

function setScreenCamSettings(width, height, fps) {
  screenCamSettings.width = width;
  screenCamSettings.height = height;
  screenCamSettings.fps = fps;
  store.set('screenCamSettings', screenCamSettings);
  applyScreenCam();
  pushState();
}

function setScreenCamSmooth(smooth) {
  screenCamSettings.smooth = smooth;
  store.set('screenCamSettings', screenCamSettings);
  applyScreenCam();
  pushState();
}

function applyZoom() {
  const f = zoomPercent / 100;
  tabs.forEach(t => {
    try { t.view.webContents.setZoomFactor(f); } catch (e) {}
  });
}

function setZoomPercent(p) {
  zoomPercent = Math.max(80, Math.min(200, p));
  store.set('zoomFactor', zoomPercent);
  applyZoom();
  pushState();
}

function detectOutputName() {
  try {
    const displays = screen.getAllDisplays();
    if (displays && displays.length) {
      const label = displays[0].label;
      if (label && /^[A-Za-z0-9]+-\d+$/.test(label)) return label;
    }
  } catch (e) {}
  try {
    const out = execSync('dms randr', { encoding: 'utf8', timeout: 3000 });
    const m = out.match(/^(\S+)\s*\(/m);
    if (m) return m[1];
  } catch (e) {}
  try {
    const out = execSync('wlr-randr', { encoding: 'utf8', timeout: 3000 });
    const m = out.match(/^([A-Za-z0-9-]+)\s/m);
    if (m) return m[1];
  } catch (e) {}
  return null;
}

function configureWlrPortal() {
  const configPath = path.join(os.homedir(), '.config', 'xdg-desktop-portal-wlr', 'config');
  const outputName = detectOutputName();
  if (!outputName) return false;
  try {
    fs.accessSync(configPath);
    return false;
  } catch (e) {}
  const content = '[screencast]\noutput_name=' + outputName + '\nchooser_type=none\n';
  try {
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
    fs.writeFileSync(configPath, content);
  } catch (e) {
    return false;
  }
  try { execSync('systemctl --user restart xdg-desktop-portal-wlr', { timeout: 5000 }); } catch (e) {}
  return true;
}

async function checkCapture() {
  let ready = false;
  try {
    const sources = await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: 0, height: 0 } });
    ready = sources.length > 0;
  } catch (e) {}
  if (!ready && process.platform === 'linux' && process.env.XDG_SESSION_TYPE === 'wayland') {
    configureWlrPortal();
    try {
      const sources = await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: 0, height: 0 } });
      ready = sources.length > 0;
    } catch (e) {}
  }
  captureReady = ready;
  pushState();
  return ready;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 900,
    minWidth: 800,
    minHeight: 600,
    title: 'AVD Dashboard',
  });
  mainWindow.setMaxListeners(0);

  mainWindow.on('resize', layout);
  mainWindow.on('closed', function() { mainWindow = null; });

  tabBarView = new BrowserView({
    webPreferences: {
      preload: path.join(__dirname, 'preload-tabbar.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  tabBarView.setBackgroundColor('#161b22');
  tabBarView.webContents.loadURL('data:text/html,' + encodeURIComponent(tabBarContent));
  mainWindow.addBrowserView(tabBarView);

  const mainTab = createTab('main', AVD_URL, 'Dashboard');
  activeTabId = 'main';
  layout();
  pushState();
}

app.on('web-contents-created', function(_event, wc) {
  wc.setWindowOpenHandler(function(details) {
    const url = details.url;
    if (url && url.indexOf('http') === 0) {
      addTab(url, details.frameName || 'VM');
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });
});

app.whenReady().then(function() {
  Menu.setApplicationMenu(null);
  store.init(app.getPath('userData'));
  screenCamSettings = store.get('screenCamSettings', { width: 1280, height: 720, fps: 30, smooth: 0 });
  if (screenCamSettings.smooth == null) screenCamSettings.smooth = 0;
  keepAliveDuration = store.get('keepAliveDuration', 30);
  blockFullscreen = store.get('blockFullscreen', false);
  zoomPercent = store.get('zoomFactor', 100);

  session.defaultSession.setPermissionRequestHandler(function(_wc, permission, callback) {
    callback(true);
  });

  session.defaultSession.setDisplayMediaRequestHandler(function(_request, callback) {
    desktopCapturer.getSources({ types: ['screen'] }).then(function(sources) {
      callback({ video: sources[0] });
    }).catch(function() { callback({}); });
  });

  ipcMain.on('switch-tab', function(_event, id) { switchTab(id); });
  ipcMain.on('get-state', function() { pushState(); });
  ipcMain.on('toggle-speaker', function() {
    const t = activeTab();
    if (!t) return;
    t.muted = !t.muted;
    t.view.webContents.setAudioMuted(t.muted);
    pushState();
  });
  ipcMain.on('toggle-mic', function() {
    const t = activeTab();
    if (!t) return;
    t.micEnabled = !t.micEnabled;
    t.view.webContents.executeJavaScript(
      'window.__workeeSetMic && window.__workeeSetMic(' + (t.micEnabled ? 'true' : 'false') + ')'
    ).catch(() => {});
    pushState();
  });
  ipcMain.on('media-state', function(event, s) {
    const t = tabForWebContents(event.sender);
    if (!t) return;
    t.micActive = !!(s && s.audio);
    t.videoActive = !!(s && s.video);
    t.videoSource = s && s.video ? (s.videoSource || null) : null;
    if (s && s.audioDevice) t.inputLabel = s.audioDevice;
    pushState();
  });
  ipcMain.on('media-devices', function(event, d) {
    const t = tabForWebContents(event.sender);
    if (!t) return;
    if (d && d.audiooutput) t.outputLabel = d.audiooutput;
    if (d && d.audioinput && !t.inputLabel) t.inputLabel = d.audioinput;
    pushState();
  });

  ipcMain.handle('screenshare-config', function() {
    return {
      signalingUrl: signalingUrl,
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
    };
  });
  ipcMain.on('screenshare-status', function(_event, status) {
    if (status && status.state) {
      screenshareConnected = status.state === 'connected';
    }
    pushState();
  });
  ipcMain.on('toggle-screenshare', function() {
    if (screensharing) stopScreenShare();
    else startScreenShare();
  });
  ipcMain.on('toggle-screencam', function() {
    screenCam = !screenCam;
    applyScreenCam();
    pushState();
  });
  ipcMain.on('screencam-settings', function() {
    const s = screenCamSettings;
    const menu = Menu.buildFromTemplate([
      { label: 'Resolution', submenu: [
        { label: '1920 \u00d7 1080', type: 'radio', checked: s.width === 1920, click: function() { setScreenCamSettings(1920, 1080, s.fps); } },
        { label: '1280 \u00d7 720', type: 'radio', checked: s.width === 1280, click: function() { setScreenCamSettings(1280, 720, s.fps); } },
        { label: '960 \u00d7 540', type: 'radio', checked: s.width === 960, click: function() { setScreenCamSettings(960, 540, s.fps); } },
        { label: '640 \u00d7 360', type: 'radio', checked: s.width === 640, click: function() { setScreenCamSettings(640, 360, s.fps); } },
      ]},
      { label: 'Frame rate', submenu: [
        { label: '30 fps', type: 'radio', checked: s.fps === 30, click: function() { setScreenCamSettings(s.width, s.height, 30); } },
        { label: '15 fps', type: 'radio', checked: s.fps === 15, click: function() { setScreenCamSettings(s.width, s.height, 15); } },
      ]},
      { label: 'Smoothing', submenu: [
        { label: 'Off', type: 'radio', checked: s.smooth === 0, click: function() { setScreenCamSmooth(0); } },
        { label: 'Light', type: 'radio', checked: s.smooth === 1, click: function() { setScreenCamSmooth(1); } },
        { label: 'Strong', type: 'radio', checked: s.smooth === 2, click: function() { setScreenCamSmooth(2); } },
      ]},
    ]);
    menu.popup({ window: mainWindow });
  });
  ipcMain.on('toggle-keepalive', function() {
    kaIntent = !kaIntent;
    tabs.forEach(function(t) {
      if (t.id === 'main') return;
      if (kaIntent) { t.kaActive = true; t.kaStart = Date.now(); }
      else { t.kaActive = false; }
    });
    pushState();
  });
  ipcMain.on('keepalive-duration', function(_event, duration) {
    keepAliveDuration = duration;
    store.set('keepAliveDuration', duration);
    tabs.forEach(function(t) {
      if (t.id === 'main' || !t.kaActive) return;
      t.kaStart = Date.now();
    });
    pushState();
  });
  ipcMain.on('keepalive-gear', function() {
    const options = [15, 30, 60, 90, 120];
    const menu = Menu.buildFromTemplate(options.map(function(d) {
      return {
        label: d + ' min',
        type: 'radio',
        checked: keepAliveDuration === d,
        click: function() { ipcMain.emit('keepalive-duration', null, d); },
      };
    }));
    menu.popup({ window: mainWindow });
  });
  ipcMain.on('keepalive-input', function(event) {
    const t = tabForWebContents(event.sender);
    if (!t || !t.kaActive) return;
    t.kaStart = Date.now();
    t.kaResetFlag = true;
  });
  ipcMain.on('app-menu', function() {
    const title = app.getName() || 'AVD Electron';
    const menu = Menu.buildFromTemplate([
      {
        label: 'About ' + title,
        click: function() {
          dialog.showMessageBox(mainWindow, {
            type: 'info',
            title: 'About ' + title,
            message: title,
            detail: 'Version ' + (app.getVersion() || 'unknown'),
          });
        },
      },
      {
        type: 'checkbox',
        label: 'Prevent auto-fullscreen',
        checked: blockFullscreen,
        click: function() {
          toggleBlockFullscreen();
        },
      },
      {
        label: 'Interface scale',
        submenu: [80, 90, 100, 110, 125, 150, 175, 200].map(function(p) {
          return {
            label: p + '%',
            type: 'radio',
            checked: zoomPercent === p,
            click: function() { setZoomPercent(p); },
          };
        }),
      },
      { type: 'separator' },
      { label: 'Quit', click: function() { app.quit(); } },
    ]);
    menu.popup({ window: mainWindow });
  });
  ipcMain.on('close-tab', function(_event, id) {
    const tab = tabs.find(t => t.id === id);
    if (!tab || id === 'main') return;
    dialog.showMessageBox(mainWindow, {
      type: 'warning',
      buttons: ['Close', 'Cancel'],
      defaultId: 1,
      cancelId: 1,
      title: 'Close VM session',
      message: 'Close "' + tab.title + '"?',
      detail: 'This will disconnect the VM session.',
    }).then(function(result) {
      if (result.response === 0) closeTab(id);
    });
  });

  setInterval(function() {
    const now = Date.now();
    const durationMs = keepAliveDuration * 60 * 1000;
    const activeTabs = [];
    tabs.forEach(function(t) {
      if (t.id === 'main' || !t.kaActive) return;
      if (now - t.kaStart >= durationMs) {
        t.kaActive = false;
      } else if (t.kaResetFlag) {
        const left = durationMs - (now - t.kaStart);
        console.log('[keep-alive] timer reset by activity on ' + (t.title || t.id) + ': will nudge for ' + fmtRemaining(left) + ' (until ' + new Date(now + left).toLocaleString() + ')');
        t.kaResetFlag = false;
        sendHarmlessKey(t.view.webContents);
        activeTabs.push(t);
      } else {
        sendHarmlessKey(t.view.webContents);
        activeTabs.push(t);
      }
    });
    if (activeTabs.length) pushState();
  }, 120000);

  createWindow();
  applyZoom();
  checkCapture();
});

app.on('window-all-closed', function() { app.quit(); });
