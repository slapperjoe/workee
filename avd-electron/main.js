const { app, BrowserWindow, BrowserView, ipcMain } = require('electron');
const path = require('path');

const AVD_URL = 'https://windows.cloud.microsoft/#/devices';

let mainWindow = null;
let tabBarView = null;
let tabs = [];
let activeTabId = 'main';
let tabIdCounter = 0;

const tabBarContent = '<!DOCTYPE html><html><head><style>' +
  'html,body{margin:0;padding:0;background:#161b22;display:flex;overflow:hidden;height:36px}' +
  '.t{display:inline-flex;align-items:center;padding:0 14px;font-size:12px;font-family:system-ui,sans-serif;' +
  'cursor:pointer;border-right:1px solid #30363d;white-space:nowrap;flex-shrink:0;color:#8b949e;height:36px}' +
  '.t:hover{background:#21262d}.t.a{background:#0d1117;color:#c9d1d9}' +
  '</style></head><body></body></html>';

function layout() {
  if (!mainWindow) return;
  const [w, h] = mainWindow.getSize();
  const th = 36;
  if (tabBarView) tabBarView.setBounds({ x: 0, y: 0, width: w, height: th });
  tabs.forEach(t => {
    try {
      if (t.id === activeTabId) {
        t.view.setBounds({ x: 0, y: th, width: w, height: h - th });
        mainWindow.addBrowserView(t.view);
      } else {
        mainWindow.removeBrowserView(t.view);
      }
    } catch (e) {}
  });
}

function renderTabs() {
  if (!tabBarView) return;
  var parts = [];
  for (var i = 0; i < tabs.length; i++) {
    var t = tabs[i];
    var active = t.id === activeTabId;
    var cls = active ? 't a' : 't';
    var name = t.title || 'VM';
    if (name.length > 20) name = name.substring(0, 18) + '..';
    parts.push('<div class="' + cls + '" onclick="window.api.switchTab(\'' + t.id + '\')">'
      + name.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>');
  }
  tabBarView.webContents.executeJavaScript(
    'document.body.innerHTML=' + JSON.stringify(parts.join('')) + ';'
  ).catch(function() {});

  // Mark active tab
  tabBarView.webContents.executeJavaScript(
    'var tabs=document.querySelectorAll(".t");' +
    'for(var i=0;i<tabs.length;i++)tabs[i].classList.remove("a");' +
    'var el=document.querySelector(".t[onclick*=\'' + activeTabId + '\']");' +
    'if(el)el.classList.add("a");'
  ).catch(function() {});
}

function switchTab(id) {
  activeTabId = id;
  layout();
  renderTabs();
}

function addTab(url, title) {
  var id = 't' + (++tabIdCounter);
  var view = new BrowserView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  tabs.push({ id: id, view: view, title: title || 'VM' });
  view.webContents.loadURL(url);
  switchTab(id);
  renderTabs();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 900,
    minWidth: 800,
    minHeight: 600,
    title: 'AVD Dashboard',
  });

  mainWindow.on('resize', layout);
  mainWindow.on('closed', function() { mainWindow = null; });

  mainWindow.webContents.setWindowOpenHandler(function(details) {
    var url = details.url;
    if (url && (url.indexOf('http') === 0)) {
      addTab(url, 'VM Session');
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  app.on('web-contents-created', function(_event, wc) {
    wc.setWindowOpenHandler(function(details) {
      var url = details.url;
      if (url && (url.indexOf('http') === 0)) {
        addTab(url, 'Session');
        return { action: 'deny' };
      }
      return { action: 'allow' };
    });
  });

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

  var mainView = new BrowserView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  tabs.push({ id: 'main', view: mainView, title: 'Dashboard' });
  activeTabId = 'main';
  mainView.webContents.loadURL(AVD_URL);
  mainWindow.addBrowserView(mainView);
  layout();
  renderTabs();
}

app.whenReady().then(function() {
  ipcMain.on('switch-tab', function(event, id) {
    switchTab(id);
  });
  createWindow();
});
app.on('window-all-closed', function() { app.quit(); });
