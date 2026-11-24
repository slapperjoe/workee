if (process.platform === 'linux' && !process.env.GDK_BACKEND) {
  process.env.GDK_BACKEND = 'x11';
}

// Guard against EPIPE on stdout/stderr. When the launcher's stdout pipe is
// closed (e.g. the wrapping interactive shell exits), a plain console.log
// throws "Error: write EPIPE" as an uncaught main-process exception and
// Electron pops its error dialog — repeatedly, on every 700ms autofill poll.
// Swallowing EPIPE keeps logging non-fatal; other stream errors still throw.
for (const _stream of [process.stdout, process.stderr]) {
  _stream.on('error', function (err) {
    if (err && err.code === 'EPIPE') return;
    throw err;
  });
}

const { app, BrowserWindow, BrowserView, ipcMain, session, desktopCapturer, Menu, dialog, screen, clipboard, globalShortcut } = require('electron');

// Single-instance guard: two processes on the same Chromium profile collide
// on LevelDB locks; the RDP WASM core then cannot open its IndexedDB store
// and the VM tab goes white ("RuntimeError: unreachable" in librdphtml.wasm).
// The losing process exits; the running one is focused instead.
if (!app.requestSingleInstanceLock()) {
  console.log('[workee] another instance is already running — exiting');
  process.exit(0);
}
app.on('second-instance', function () {
  setTimeout(function () {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  }, 100);
});

const path = require('path');
const fs = require('fs');
const store = require('./store');
const monitor = require('./monitor');
monitor.init();
const credentials = require('./credentials');
const autofill = require('./autofill');

const AVD_URL = 'https://windows.cloud.microsoft/#/devices';

let mainWindow = null;
let tabBarView = null;
let tabs = [];
let activeTabId = 'main';
let tabIdCounter = 0;
let autoFillCreds = false;
let credentialsWindow = null;
const interstitialScheduled = new Set();

let screenshareWindow = null;
let screensharing = false;
let screenshareConnected = false;
let keepAliveDuration = 30;
let kaIntent = false;
let blockFullscreen = false;
let zoomPercent = 100;
// Auto-hide tab bar: when enabled the bar collapses to a small pennant pinned in
// the top-left corner; dwelling on it re-opens the bar, and the pointer leaving
// the bar collapses it again after GRACE_MS.
// Widened for the two move arrows; the flag is the only place those controls exist when the bar
// is auto-hidden, so they have to fit here.
const FLAG_W = 100;
// Collapsed width: the grab handle only, so nothing in the corner is shown until the pointer
// is on it or it is clicked. A BrowserView background cannot be made reliably transparent, so
// the flag is sized to what is visible instead of painting an invisible area.
const FLAG_MIN_W = 24;
const FLAG_H = 18;
const GRACE_MS = 3000;
let autoHideTabBar = false;
let barHidden = false;
let flagExpanded = false;
let flagView = null;
let collapseTimer = null;
const signalingUrl = process.env.SCREENSHARE_SIGNALING_URL || 'ws://localhost:8080';

// Main-process errors normally pop Electron's own dialog, whose text cannot be
// selected or copied. Show ours instead: log it, put it on the clipboard, and keep a
// Copy button in case the clipboard was busy.
function reportError(where, err) {
  const text = String((err && err.stack) || (err && err.message) || err);
  console.error('[error] ' + where + ': ' + text);
  try { clipboard.writeText(where + ': ' + text); } catch (e) {}
  if (!mainWindow || mainWindow.isDestroyed()) return;
  dialog.showMessageBox(mainWindow, {
    type: 'error',
    title: 'Error',
    message: 'Error in ' + where,
    detail: text + '\n\n(already copied to clipboard)',
    buttons: ['OK', 'Copy again'],
    defaultId: 0,
  }).then(function (result) {
    if (result.response === 1) {
      try { clipboard.writeText(where + ': ' + text); } catch (e) {}
    }
  }).catch(function () {});
}

process.on('uncaughtException', function (err) {
  if (err && err.code === 'EPIPE') return;
  reportError('uncaught exception', err);
});


let mediaHook = '';
try {
  mediaHook = fs.readFileSync(path.join(__dirname, 'media-hook.js'), 'utf8');
} catch (e) {}

let screenShim = '';
try {
  screenShim = fs.readFileSync(path.join(__dirname, 'screen-shim.js'), 'utf8');
} catch (e) {}

// One-window-per-monitor (experimental). The AVD client's own multi-monitor path is
// window-per-monitor, but it only runs when the page reports two screens and a window on
// each; Electron gives us neither, so screen-shim.js supplies a virtual two-screen world
// and the popup.html request is honoured as a real window instead of a tab.
let splitWindowsOn = false;
let fakeWideGate = false;
let uaOverride = false;
// Flags the client reads from the flight list; this tenant reports them false, which is
// what disables the multi-monitor control regardless of the screen or gate. The shim
// rewrites the flight response when this is on. See screen-shim.js.
// Nh_Split_Screen stays false: in the SplitScreen scenario the client maps every pointer event
// against canvasList[0] (onCanvasPosition), so input inside the second window's canvas is measured
// from the primary canvas at the top-left and lands in the wrong place. The MultiMonitor scenario
// uses the canvas the event actually came from, which is the one this two-window layout needs.
const FLIGHT_FLAGS = {
  'Nh_Multi_Monitor': true,
  'Nh_Split_Screen': false,
  'Nh_Enable_Windowed_Secondary_Monitor': true,
};
let flightFlagsOn = false;
// The client's supported-platform check is a user-agent test plus three API-presence
// checks (AudioContext, getUserMedia, serviceWorker) that Electron passes. Only the UA
// fails, and it gates the display settings panel. This is a deliberate override behind an
// option, not a blanket change - the earlier UA/client-hint experiment was reverted for
// the MFA path and stays reverted unless this option is on.
const CHROME_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
let DEFAULT_UA = null;

function applyUserAgent() {
  if (!DEFAULT_UA) DEFAULT_UA = session.defaultSession.userAgent;
  try {
    session.defaultSession.setUserAgent(uaOverride ? CHROME_UA : DEFAULT_UA);
    console.log('[ua] ' + (uaOverride ? 'chrome ua applied - reload the tab for the client to see it' : 'default ua restored'));
  } catch (e) {
    console.warn('[ua] could not set user agent: ' + e.message);
  }
  if (uaOverride) {
    // The Chrome UA must not reach Microsoft's sign-in pages. With a Chrome-looking UA the
    // account page takes its WebAuthn/FIDO path (login.microsoft.com/common/fido/get), and
    // Electron has no native passkey UI to render, so the sign-in stalls on a blank page.
    // navigator.userAgent stays Chrome for the client's own platform check; only the
    // requests to the login hosts are given back Electron's real UA.
    session.defaultSession.webRequest.onBeforeSendHeaders(function (details, callback) {
      if (/login\.microsoft(online)?\.com/.test(details.url) && details.requestHeaders) {
        details.requestHeaders['User-Agent'] = DEFAULT_UA;
      }
      callback({ requestHeaders: details.requestHeaders });
    });
  }
}
const splitOwners = new Map(); // tabId -> BrowserWindow
let pendingSplitOwner = null;

// Order is deliberate, not left-to-right. The client's settings control is
// `disabled:!U` where U is MultiMonitor.firstMonitorIndex, and that index comes from
// Hn(window) - the screen whose availLeft/availTop matches the window's reported screen.
// With one physical display that is always 0, so the control is disabled. Reporting the
// main window as index 1 and the split window as index 0 is what makes it enabled.
// Geometry is unaffected: Qn sizes each monitor from that window's own canvas rect.
function virtualScreens() {
  const d = (mainWindow && !mainWindow.isDestroyed() ? screen.getDisplayMatching(mainWindow.getBounds()) : null) || screen.getPrimaryDisplay();
  const half = Math.floor(d.workArea.width / 2);
  return [
    { index: 0, id: d.id + '-workee-split', left: d.workArea.x + half, top: d.workArea.y, width: d.workArea.width - half, height: d.workArea.height, deviceScaleFactor: d.scaleFactor },
    { index: 1, id: d.id + '-workee-main', left: d.workArea.x, top: d.workArea.y, width: half, height: d.workArea.height, deviceScaleFactor: d.scaleFactor },
  ];
}

function injectShim(wc, index) {
  if (!splitWindowsOn || !screenShim || !wc || wc.isDestroyed()) return;
  // Read per call rather than once at startup: the shim's effect lives in the page, so a
  // tab reload picks up any change to it without restarting the app (a restart costs a
  // sign-in).
  const src = (function () {
    try { return fs.readFileSync(path.join(__dirname, 'screen-shim.js'), 'utf8'); } catch (e) { return screenShim; }
  })();
  const code = 'window.__workeeScreens=' + JSON.stringify(virtualScreens()) +
    ';window.__workeeScreenIndex=' + index +
    ';window.__workeeGateMode=' + (fakeWideGate ? "'double'" : 'null') +
    ';window.__workeeFlightFlags=' + (flightFlagsOn ? JSON.stringify(FLIGHT_FLAGS) : 'null') +    ';' + src;
  try {
    wc.executeJavaScript(code).catch(function () {});
  } catch (e) {}
}

// Note: the shim can only add the fake screen world, it cannot take it back out of a
// page that already ran it - turning the option off needs the tab reloaded.
function reInjectShims() {
  tabs.forEach(function (t) { injectShim(t.view.webContents, 1); });
  splitOwners.forEach(function (w, tabId) {
    if (w && !w.isDestroyed()) injectShim(w.webContents, 0);
  });
}

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

// The corner handle shown when the bar is auto-hidden. The pennant itself is the
// only thing in the document, so a pointerenter on the page == a hover on the flag.
const flagContent = `<!DOCTYPE html><html><head><style>
  html,body{margin:0;padding:0;height:100%;background:#161b22;overflow:hidden}
  #flag{display:flex;align-items:center;gap:5px;box-sizing:border-box;height:100%;padding:0 5px;cursor:pointer;border-right:1px solid #30363d;border-bottom:1px solid #30363d;border-radius:0 5px 0 0}
  #flag svg{width:13px;height:13px;fill:#8b949e}
  #flag .dot{width:6px;height:6px;border-radius:50%;background:#30363d}
  #flag:hover{background:#21262d}
  #flag:hover svg{fill:#c9d1d9}
  #flag:hover .dot{background:#58a6ff}
  #flag .glyph, #flag .mv{display:none}
  #flag.open .glyph{display:flex;align-items:center;gap:5px}
  #flag.open .mv{display:inline}
  #flag .mv{font-size:13px;line-height:1;color:#8b949e;cursor:pointer;padding:0 3px}
  #flag .mv:hover{color:#58a6ff}
  #flag .grab svg{width:12px;height:12px;stroke:#8b949e;fill:none;stroke-width:2}
  #flag:hover .grab svg{stroke:#58a6ff}
  </style></head><body>
  <div id="flag" title="Tab bar hidden - hover here to show it"><span class="glyph"><svg viewBox="0 0 24 24"><path d="M5 2h2v20H5z"/><path d="M7 2l11 4-11 4z"/></svg><span class="dot"></span></span><span class="mv" data-dir="left" title="Move the focused window to the left monitor">&#9664;</span><span class="mv" data-dir="right" title="Move the focused window to the right monitor">&#9654;</span><span class="grab" title="Grab: show the tab bar"><svg viewBox="0 0 24 24"><path d="M7 5v14M12 5v14M17 5v14"/></svg></span></div>
  </body></html>`;

function activeTab() {
  for (let i = 0; i < tabs.length; i++) if (tabs[i].id === activeTabId) return tabs[i];
  return null;
}

// Devtools for whichever page has focus: the split window, or the active tab's view.
// Needed because the client's "please respond to browser permission requests" hint is driven
// by an internal pending-request counter, and only the page itself can say which call is stuck.
function openDevtools() {
  let wc = null;
  const w = BrowserWindow.getFocusedWindow();
  if (w && !w.isDestroyed()) wc = w.webContents;
  if (!wc) {
    const t = activeTab();
    if (t && t.view) wc = t.view.webContents;
  }
  if (!wc) return;
  console.log('[devtools] opened for ' + (wc.getTitle() || wc.getURL().slice(0, 60)));
  wc.openDevTools({ mode: 'detach' });
}

function tabForWebContents(wc) {
  for (let i = 0; i < tabs.length; i++) if (tabs[i].view && tabs[i].view.webContents === wc) return tabs[i];
  return null;
}

function layout() {
  if (!mainWindow) return;
  const [w, h] = mainWindow.getSize();
  const hidden = autoHideTabBar && barHidden;
  const th = hidden ? 0 : 36;
  if (hidden) {
    // The flag floats over the page corner, so the tab can use the full height.
    if (tabBarView) tabBarView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    if (flagView) {
      flagView.setBounds({ x: 0, y: 0, width: flagExpanded ? FLAG_W : FLAG_MIN_W, height: FLAG_H });
      try { mainWindow.setTopBrowserView(flagView); } catch (e) {}
    }
  } else {
    if (tabBarView) tabBarView.setBounds({ x: 0, y: 0, width: w, height: 36 });
    if (flagView) flagView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    try { mainWindow.setTopBrowserView(tabBarView); } catch (e) {}
  }
  tabs.forEach(t => {
    if (t.id === activeTabId) {
      t.view.setBounds({ x: 0, y: th, width: w, height: h - th });
    } else {
      t.view.setBounds({ x: 0, y: th, width: 0, height: 0 });
    }
  });
}

function showTabBar() {
  if (collapseTimer) { clearTimeout(collapseTimer); collapseTimer = null; }
  if (!barHidden) return;
  barHidden = false;
  // Opening the bar ends the hover state; a later collapse starts collapsed again.
  flagExpanded = false;
  layout();
  pushState();
}

function hideTabBar() {
  if (!autoHideTabBar || barHidden) return;
  barHidden = true;
  layout();
  pushState();
}

function pushState() {
  // The view object survives its own destruction, so checking it is not enough: during
  // shutdown a closed-window handler can call pushState after the webContents is gone.
  if (!tabBarView || !tabBarView.webContents || tabBarView.webContents.isDestroyed()) return;
  const t = activeTab();
  const state = {
    tabs: tabs.map(x => ({ id: x.id, title: x.title, active: x.id === activeTabId })),
    speaker: { muted: t ? t.muted : false, audible: t ? t.audible : false },
    mic: { enabled: t ? t.micEnabled : true, active: t ? t.micActive : false },
    videoSource: t ? t.videoSource : null,
    devices: { input: t ? t.inputLabel : '', output: t ? t.outputLabel : '' },
    screenshare: { on: screensharing, connected: screenshareConnected },
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
  if (url.indexOf('/webclient/') >= 0) {
    tab.isWebclient = true;
    if (!interstitialScheduled.has(wc.id)) {
      interstitialScheduled.add(wc.id);
      autofill.scheduleInterstitialCheck(wc);
    }
  }
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
    injectShim(wc, 1);
    if (!mediaHook) return;
    wc.executeJavaScript(mediaHook).then(() => {
      return wc.executeJavaScript(
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

function openCredentialsWindow() {
  if (credentialsWindow && !credentialsWindow.isDestroyed()) {
    credentialsWindow.show();
    credentialsWindow.focus();
    return;
  }
  credentialsWindow = new BrowserWindow({
    width: 420,
    height: 300,
    resizable: false,
    modal: true,
    parent: mainWindow,
    title: 'VM sign-in credentials',
    webPreferences: {
      preload: path.join(__dirname, 'preload-credentials.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  credentialsWindow.loadFile(path.join(__dirname, 'credentials.html'));
  credentialsWindow.webContents.on('dom-ready', function() {
    credentialsWindow.webContents.send('init', {
      email: store.get('credentialEmail', ''),
      hasPassword: credentials.available(),
      backend: credentials.getBackend(),
    });
  });
  credentialsWindow.on('closed', function() { credentialsWindow = null; });
}

function createWindow() {
  // The app does not size this window for the split path. On Wayland a client cannot move
  // or resize its own window after it is mapped, so neither a creation-time half nor a later
  // setBounds is reliably honoured, and the client's own AdjustChildWindow path (exitFullscreen
  // then resizeTo/moveTo) is the thing that would place a window on a screen. Placement is the
  // user's or the compositor's job here; the app's job is the virtual screen world, the flags,
  // and the fullscreen state. Each remote monitor is sized from whatever that window actually is.
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

  flagView = new BrowserView({
    webPreferences: {
      preload: path.join(__dirname, 'preload-tabbar.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  flagView.setBackgroundColor('#161b22');
  flagView.webContents.loadURL('data:text/html,' + encodeURIComponent(flagContent));
  mainWindow.addBrowserView(flagView);

  const mainTab = createTab('main', AVD_URL, 'Dashboard');
  activeTabId = 'main';
  layout();
  pushState();
}

app.on('web-contents-created', function(_event, wc) {
  autofill.onWebContents(wc);
  wc.setWindowOpenHandler(function(details) {
    const url = details.url;
    if (url && url.indexOf('http') === 0) {
      // The client's multi-monitor path opens its second window through window.open;
      // it needs the returned Window object, so this one must actually be allowed.
      if (splitWindowsOn && url.indexOf('/popup.html') >= 0) {
        const owner = tabForWebContents(wc);
        // One window per tab. The client re-issues window.open on every scenario
        // re-evaluation and on reconnect, and each call created another window - the old
        // ones stayed open but were no longer mapped to the tab, so they accumulated.
        // A repeat open means the client has a fresh reference and no longer uses the one
        // we mapped, so the old window is orphaned rather than reused: denying here would
        // hand the client a null Window and it cannot build anything in it. Close the stale
        // window and let this open create the one the client actually holds.
        const existing = owner && splitOwners.get(owner.id);
        if (existing && !existing.isDestroyed()) {
          console.log('[split] repeat popup request for tab ' + owner.id + ' - closing the stale window before the new one (' + splitOwners.size + ' mapped)');
          try { existing.close(); } catch (e) {}
        }
        pendingSplitOwner = owner;
        const half = virtualScreens()[0];
        // Create the window at its half instead of taking the client's features string
        // (default size) and resizing afterwards. Qn() measures each monitor from that
        // window's own position and its container rect, so a window that is still the
        // default size when the client measures it produces a full-display monitor.
        console.log('[split] popup request allowed for tab ' + (pendingSplitOwner ? pendingSplitOwner.id : 'none') +
          ' as a real window, created at ' + half.left + ',' + half.top + ' ' + half.width + 'x' + half.height);
        return { action: 'allow', overrideBrowserWindowOptions: { x: half.left, y: half.top, width: half.width, height: half.height } };
      }
      addTab(url, details.frameName || 'VM');
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });
});

function attachSplitWindow(w) {
  const tab = pendingSplitOwner;
  pendingSplitOwner = null;
  if (!tab) return;
  splitOwners.set(tab.id, w);
  const screens = virtualScreens();
  console.log('[split] virtual screens: ' + JSON.stringify(screens));
  // screens[0] is the split window's screen, screens[1] the main window's (see virtualScreens).
  try { w.setBounds({ x: screens[0].left, y: screens[0].top, width: screens[0].width, height: screens[0].height }); } catch (e) {}
  // What the client will measure is the window's own position and its container rect, so
  // log what it actually is rather than what we asked for.
  try { console.log('[split] split window bounds: ' + JSON.stringify(w.getBounds()) + ' page DPR: ' + w.webContents.getZoomFactor()); } catch (e) {}
  // Only reported, not applied: the app cannot place windows on Wayland. This log is what
  // tells us what the app asked for versus what the client then measured.
  console.log('[split] requested layout (not applied on Wayland): main ' + screens[1].width + 'x' + screens[1].height +
    ' at ' + screens[1].left + ', split ' + screens[0].width + 'x' + screens[0].height + ' at ' + screens[0].left);
  const wc = w.webContents;
  try { wc.setZoomFactor(zoomPercent / 100); } catch (e) {}
  // The client asks the child window for its screen details right after the open call,
  // so the fake screen has to be in place at creation, not after the page parses.
  injectShim(wc, 0);
  wc.on('dom-ready', function () { injectShim(wc, 0); });
  w.on('closed', function () {
    console.log('[split] second window closed - rejoin handled by the client (its own beforeunload path)');
    // Closing the child is the rejoin: the client's own handler runs off its
    // beforeunload and sets secondMonitorIndex back to -1.
    splitOwners.delete(tab.id);
    pushState();
  });
  pushState();
}

app.on('browser-window-created', function (_event, w) {
  if (!splitWindowsOn || !pendingSplitOwner) return;
  attachSplitWindow(w);
});

app.whenReady().then(function() {
  Menu.setApplicationMenu(null);
  // On Wayland a global shortcut can be refused (the compositor owns the key), so record whether
  // it was actually taken. The menu item below is the reliable path.
  const ok = globalShortcut.register('CommandOrControl+Shift+I', openDevtools);
  console.log('[devtools] shortcut ' + (ok ? 'registered' : 'NOT registered - use the menu item'));
  store.init(app.getPath('userData'));
  keepAliveDuration = store.get('keepAliveDuration', 30);
  kaIntent = store.get('keepAliveOn', false);
  blockFullscreen = store.get('blockFullscreen', false);
  autoHideTabBar = store.get('autoHideTabBar', false);
  barHidden = autoHideTabBar;
  zoomPercent = store.get('zoomFactor', 100);
  autoFillCreds = store.get('autoFillCredentials', false);
  splitWindowsOn = store.get('splitWindows', false);
  fakeWideGate = store.get('fakeWideGate', false);
  uaOverride = store.get('uaOverride', false);
  flightFlagsOn = store.get('flightFlags', false);
  DEFAULT_UA = session.defaultSession.userAgent;
  if (uaOverride) applyUserAgent();

  if (flightFlagsOn) {
    // Log which request carries the flight list, so it is clear whether the page-side
    // rewrite in screen-shim.js was installed before or after it.
    session.defaultSession.webRequest.onBeforeRequest(function (details, callback) {
      if (/flight/i.test(details.url)) console.log('[flight] request: ' + details.url);
      callback({ cancel: false });
    });
  }

  credentials.init(app.getPath('userData'));
  console.log('[credentials] backend: ' + credentials.getBackend());
  console.log('[workee] options: splitWindows=' + splitWindowsOn + ' gateMode=' + (fakeWideGate ? 'double' : 'off') +
    ' uaOverride=' + uaOverride + ' flightFlags=' + flightFlagsOn + ' autoHideTabBar=' + autoHideTabBar + ' keepAliveOn=' + kaIntent);
  autofill.init({
    enabled: function() { return autoFillCreds; },
    getPassword: function() { return credentials.available() ? credentials.load() : null; },
    getEmail: function() { return store.get('credentialEmail', ''); },
    getFallbackOpener: function() {
      const t = activeTab();
      if (t && t.view && t.isWebclient) return t.view.webContents;
      for (let i = 0; i < tabs.length; i++) if (tabs[i].isWebclient) return tabs[i].view.webContents;
      return null;
    },
  });

  // Which permission the page actually asked for, and from which page. Electron answers these
  // without any UI, so a client that waits for a prompt never gets one - this log is what tells
  // us the difference between "nothing was requested" and "a request was answered silently".
  session.defaultSession.setPermissionRequestHandler(function(wc, permission, callback) {
    const t = tabForWebContents(wc);
    console.log('[permission] ' + permission + ' requested by ' +
      (t ? 'tab ' + t.id : 'window ' + (wc.getTitle() || wc.getURL().slice(0, 60))));
    callback(true);
  });

  session.defaultSession.setDisplayMediaRequestHandler(function(_request, callback) {
    desktopCapturer.getSources({ types: ['screen'] }).then(function(sources) {
      callback({ video: sources[0] });
    }).catch(function() { callback({}); });
  });

  // Send Super+Shift+Arrow to the guest, which is how Windows moves the focused window to the
  // neighbouring monitor. The host compositor owns the Super key, so the app has to inject it.
  // sendInputEvent produces a trusted key event but with no key identity (probe24), so the only
  // usable form is a synthetic KeyboardEvent - that reaches window and document listeners
  // (probe25); whether the WASM core acts on an untrusted event is the open question.
  ipcMain.on('move-focus', function(_event, dir) {
    const side = dir === 'left' ? 'ArrowLeft' : 'ArrowRight';
    // Target the tab we are looking at - that page owns virtual monitor 0 and is the one
    // the client moves a guest window relative to. The second window can get its own
    // arrows later.
    let wc = null, label = '';
    const t = activeTab();
    if (t && t.view) { wc = t.view.webContents; label = 'tab ' + t.id; }
    if (!wc) {
      const w = BrowserWindow.getFocusedWindow();
      if (w && !w.isDestroyed()) { wc = w.webContents; label = 'window ' + (w.getTitle() || 'untitled'); }
    }
    if (!wc) return;
    const js = "(() => { const t = document.activeElement || document;" +
      "const mk = (type, key, code, kc) => { const e = new KeyboardEvent(type, {key: key, code: code, shiftKey: true, metaKey: true, bubbles: true});" +
      "if (kc) { Object.defineProperty(e, 'keyCode', {value: kc}); Object.defineProperty(e, 'which', {value: kc}); } return e; };" +
      "t.dispatchEvent(mk('keydown', 'Meta', 'Meta', 91));" +
      "t.dispatchEvent(mk('keydown', 'Shift', 'ShiftLeft', 16));" +
      "t.dispatchEvent(mk('keydown', '" + side + "', '" + side + "', " + (side === 'ArrowLeft' ? 37 : 39) + "));" +
      "t.dispatchEvent(mk('keyup', '" + side + "', '" + side + "', " + (side === 'ArrowLeft' ? 37 : 39) + "));" +
      "t.dispatchEvent(mk('keyup', 'Shift', 'ShiftLeft', 16));" +
      "t.dispatchEvent(mk('keyup', 'Meta', 'Meta', 91));" +
      "document.body.setAttribute('data-workee-move', '" + side + "'); })()";
    console.log('[move] Super+Shift+' + side + ' -> ' + label);
    try { wc.executeJavaScript(js, true); } catch (e) { console.log('[move] failed: ' + e.message); }
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
  ipcMain.on('toggle-keepalive', function() {
    kaIntent = !kaIntent;
    store.set('keepAliveOn', kaIntent);
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
  ipcMain.on('credentials-clear', function() {
    credentials.clear();
    store.set('credentialEmail', '');
  });
  ipcMain.on('credentials-save', function(_event, d) {
    if (d && d.password) {
      credentials.save(d.password);
    }
    if (d && d.email != null) store.set('credentialEmail', d.email);
  });
  ipcMain.on('credentials-close', function() {
    if (credentialsWindow && !credentialsWindow.isDestroyed()) credentialsWindow.close();
  });

  ipcMain.on('tabbar-show', function() { showTabBar(); });
  ipcMain.on('flag-open', function() {
    if (flagExpanded) return;
    flagExpanded = true;
    layout();
  });
  ipcMain.on('flag-close', function() {
    if (!flagExpanded) return;
    flagExpanded = false;
    layout();
  });
  ipcMain.on('tabbar-enter', function() {
    if (collapseTimer) { clearTimeout(collapseTimer); collapseTimer = null; }
  });
  ipcMain.on('tabbar-leave', function() {
    if (!autoHideTabBar || barHidden) return;
    if (collapseTimer) clearTimeout(collapseTimer);
    collapseTimer = setTimeout(function() {
      collapseTimer = null;
      hideTabBar();
    }, GRACE_MS);
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
        label: 'VM sign-in credentials…',
        click: function() { openCredentialsWindow(); },
      },
      {
        type: 'checkbox',
        label: 'Auto-fill VM credentials',
        checked: autoFillCreds,
        click: function() {
          autoFillCreds = !autoFillCreds;
          store.set('autoFillCredentials', autoFillCreds);
          console.log('[autofill] auto-fill ' + (autoFillCreds ? 'enabled' : 'disabled'));
        },
      },
      {
        type: 'checkbox',
        label: 'Auto-hide tab bar',
        checked: autoHideTabBar,
        click: function() {
          autoHideTabBar = !autoHideTabBar;
          store.set('autoHideTabBar', autoHideTabBar);
          barHidden = autoHideTabBar;
          layout();
          pushState();
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
      { type: 'separator' },
      {
        label: 'One window per monitor (experimental)',
        submenu: [
          {
            type: 'checkbox',
            label: 'Use the client window-per-monitor path',
            checked: splitWindowsOn,
            click: function() {
              splitWindowsOn = !splitWindowsOn;
              store.set('splitWindows', splitWindowsOn);
              reInjectShims();
            },
          },
          {
            type: 'checkbox',
            label: 'Report double width to the ≥1600 gate (UI only)',
            checked: fakeWideGate,
            click: function() {
              fakeWideGate = !fakeWideGate;
              store.set('fakeWideGate', fakeWideGate);
              reInjectShims();
            },
          },
          {
            type: 'checkbox',
            label: 'Report a Chrome user agent (needs tab reload)',
            checked: uaOverride,
            click: function() {
              uaOverride = !uaOverride;
              store.set('uaOverride', uaOverride);
              applyUserAgent();
            },
          },
          {
            type: 'checkbox',
            label: 'Force flight flags (needs tab reload)',
            checked: flightFlagsOn,
            click: function() {
              flightFlagsOn = !flightFlagsOn;
              store.set('flightFlags', flightFlagsOn);
              reInjectShims();
            },
          },
          {
            label: 'Inspect the second window',
            enabled: splitOwners.has(activeTabId),
            click: function() {
              const w = splitOwners.get(activeTabId);
              if (!w || w.isDestroyed()) return;
              w.webContents.openDevTools({ mode: 'detach' });
            },
          },
          {
            label: 'Reload the second window',
            enabled: splitOwners.has(activeTabId),
            click: function() {
              const w = splitOwners.get(activeTabId);
              if (!w || w.isDestroyed()) return;
              // The shim is read from disk per injection, so this re-runs whatever the
              // current screen-shim.js contains - no app restart needed to test a change.
              injectShim(w.webContents, 0);
              w.webContents.reload();
            },
          },
          {
            label: 'Rejoin split windows',
            enabled: splitOwners.has(activeTabId),
            click: function() {
              const w = splitOwners.get(activeTabId);
              if (w && !w.isDestroyed()) w.close();
            },
          },
        ],
      },
      { type: 'separator' },
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
});

app.on('window-all-closed', function() { app.quit(); });
