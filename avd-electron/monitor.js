// Event/action monitor for workee.
//
// Enabled with WORKEE_MONITOR=1. Appends timestamped lines to
// $WORKEE_MONITOR_LOG (default: <os.tmpdir()>/workee-monitor.log).
//
// Captures, for every webContents: load/navigate events, titles,
// dom-ready, popup (window.open) requests AND the app's decision on
// them, webview attachments, new-contents, renderer console messages,
// and HTTP auth challenges (the "new window to enter credentials"
// path — native basic-auth login dialog). Also logs renderer→main IPC
// traffic and app lifecycle events.
//
// Deliberately non-intrusive: no preventDefault anywhere, handlers are
// observability-only, passwords are never written to the log.
'use strict';

const { app, ipcMain } = require('electron');
const fs = require('fs');
const os = require('os');
const path = require('path');

const enabled = process.env.WORKEE_MONITOR === '1';
let stream = null;
let inited = false;
const probeSeen = new Map(); // wcId -> last probed login URL

function line(kind, obj) {
  if (!stream) return;
  let payload = '';
  if (obj !== undefined && obj !== null) {
    try {
      payload = typeof obj === 'string'
        ? obj
        : JSON.stringify(obj, function (_k, v) {
            if (v === undefined) return null;
            if (typeof v === 'string' && v.length > 400) return v.slice(0, 400) + '…';
            return v;
          });
    } catch (e) {
      payload = String(obj);
    }
  }
  stream.write('[' + new Date().toISOString() + '] ' + kind + (payload ? ' ' + payload : '') + '\n');
}

function shortWc(wc) {
  try {
    let s = 'wc#' + wc.id;
    const t = wc.getTitle();
    if (t) s += ' "' + String(t).slice(0, 60) + '"';
    const u = wc.getURL();
    if (u) s += ' ' + String(u).slice(0, 200);
    return s;
  } catch (e) {
    return 'wc#?';
  }
}

// DOM probe for Microsoft login pages. Runs in the main world of the
// target webContents; collects inputs/buttons/headings (and same-origin
// iframes — MS often splits email/password fields across a same-origin
// iframe) so the log shows the real field selectors for auto-fill.
const LOGIN_DOM_PROBE = '(' +
  'function () {' +
  '  var out = { title: document.title, url: location.href, frames: 0 };' +
  '  function collect(doc, tag) {' +
  '    (out[tag] = out[tag] || []);' +
  '    var els = doc.querySelectorAll(' +
  '      "input,button,[role=button],select,h1,h2,h3,a" + (tag === "inputs" ? "" : "")' +
  '    );' +
  '    els.forEach(function (el) {' +
  '      var t = (el.innerText || el.value || "").trim().slice(0, 80);' +
  '      if (el.tagName === "INPUT") {' +
  '        out.inputs.push({' +
  '          type: el.type, name: el.name || null, id: el.id || null,' +
  '          placeholder: el.placeholder || null,' +
  '          aria: el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || null,' +
  '          visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)' +
  '        });' +
  '      } else if (el.tagName === "SELECT") {' +
  '        out["selects"] = out["selects"] || [];' +
  '        out["selects"].push({ name: el.name || null, id: el.id || null, options: Array.prototype.slice.call(doc.querySelectorAll("option")).map(function (o) { return o.value + ":" + (o.text || "").trim().slice(0, 40); }) });' +
  '      } else if (el.tagName === "BUTTON" || el.getAttribute("role") === "button") {' +
  '        if (t) out["buttons"] = (out["buttons"] = out["buttons"] || []), out["buttons"].push({ id: el.id || null, cls: (el.className || "").toString().slice(0, 60), text: t });' +
  '      } else if (/^H[1-3]$/.test(el.tagName)) {' +
  '        if (t) (out["headings"] = out["headings"] || []).push(t);' +
  '      } else if (el.tagName === "A") {' +
  '        if (t) (out["links"] = out["links"] || []).push({ href: (el.getAttribute("href") || "").slice(0, 120), text: t });' +
  '      }' +
  '    });' +
  '  }' +
  '  collect(document, "inputs");' +
  '  var frames = document.querySelectorAll("iframe");' +
  '  for (var i = 0; i < frames.length; i++) {' +
  '    var fd = null;' +
  '    try { fd = frames[i].contentDocument; } catch (e) {}' +
  '    if (fd) { out.frames++; collect(fd, "inputs"); }' +
  '  }' +
  '  return out;' +
  '}());'

function probeLoginPage(wc) {
  let u = null;
  try { u = wc.getURL() || ''; } catch (e) {}
  if (u.indexOf('login.microsoftonline.com') < 0) return;
  // Dedupe: same wc + same path?query (ignoring the ever-changing
  // redirect hash) is probed once per navigation.
  var key = u.split('#')[0];
  var last = probeSeen.get(wc.id);
  if (last === key) return;
  probeSeen.set(wc.id, key);
  try {
    wc.executeJavaScript(LOGIN_DOM_PROBE).then(function (r) {
      line('login-dom', { wc: wc.id, url: String(u).slice(0, 200), probe: r });
    }).catch(function () {});
  } catch (e) {}
}

function hookWebContents(wc) {
  try {
    // Wrap setWindowOpenHandler so every popup request is logged
    // regardless of which handler the app installs (registered before
    // main.js attaches its own, so the wrapper is always in place).
    const origSet = wc.setWindowOpenHandler.bind(wc);
    wc.setWindowOpenHandler = function (handler) {
      line('window-open-handler-installed', { wc: wc.id });
      return origSet(function (details) {
        line('window-open-request', {
          wc: wc.id,
          url: (details && details.url) || null,
          frameName: (details && details.frameName) || null,
          features: (details && details.features) || null,
          disposition: (details && details.disposition) || null,
          referrerOrigin: (details && details.referrer) ? String(details.referrer.origin || '') : null,
        });
        let ret;
        try {
          ret = handler ? handler(details) : { action: 'allow' };
        } catch (e) {
          line('window-open-handler-error', { error: String((e && e.message) || e) });
          return { action: 'deny' };
        }
        line('window-open-decision', {
          url: (details && details.url) || null,
          action: (ret && ret.action) || null,
          overrideURL: (ret && ret.url) || null,
        });
        return ret;
      });
    };

    wc.on('did-start-loading', function () { line('load-start', shortWc(wc)); });
    wc.on('did-finish-load', function () { line('load-finish', shortWc(wc)); });
    wc.on('did-navigate', function (_e, url, isInPlace, status) {
      line('navigate', { wc: wc.id, url: url, inPlace: isInPlace, status: status });
    });
    wc.on('will-navigate', function (_e, url) {
      line('will-navigate', { wc: wc.id, url: url });
    });
    wc.on('did-navigate-in-page', function (_e, url, main) {
      line('nav-in-page', { wc: wc.id, url: url, main: main });
    });
    wc.on('did-frame-navigate', function (_e, url, _p, _r, status, main) {
      line('frame-navigate', { wc: wc.id, url: url, status: status, main: main });
    });
    wc.on('page-title-updated', function (_e, title) {
      line('title', { wc: wc.id, title: String(title || '').slice(0, 100) });
    });
    wc.on('dom-ready', function () { line('dom-ready', shortWc(wc)); });
    wc.on('render-process-gone', function (_e, d) {
      line('render-process-gone', { wc: wc.id, reason: d && d.reason, exitCode: d && d.exitCode });
    });
    wc.on('unresponsive', function () { line('unresponsive', { wc: wc.id }); });
    wc.on('responsive', function () { line('responsive', { wc: wc.id }); });
    wc.on('did-attach-webview', function (_e, view) {
      let vu = null;
      try { vu = view && view.getURL ? view.getURL() : null; } catch (e) {}
      line('webview-attached', { host: wc.id, view: vu });
    });
    wc.on('add-new-contents', function (_e, contents, url) {
      line('new-contents', { host: wc.id, url: url, newId: contents && contents.id });
    });
    wc.on('did-create-window', function () {
      line('did-create-window', { wc: wc.id });
    });
    wc.on('console-message', function (e, level, message, lineNo, source) {
      line('console', { wc: wc.id, level: level, msg: String(message || '').slice(0, 300), line: lineNo, src: source });
    });
    // HTTP basic/NTLM challenge => native login dialog (separate window
    // with username/password fields). This is the strongest candidate
    // for "a new window opens to enter credentials". Logged without
    // preventDefault so the default dialog still shows; response
    // credentials are never logged.
    wc.on('login', function (_e, request, authInfo) {
      let url = null;
      let method = null;
      try {
        if (request) { url = request.getUrl(); method = request.getMethod(); }
      } catch (err) {}
      line('auth-request', {
        wc: wc.id,
        url: url,
        method: method,
        isProxy: !!(authInfo && authInfo.isProxy),
        scheme: authInfo && authInfo.scheme,
        host: authInfo && authInfo.host,
        port: authInfo && authInfo.port,
        realm: (authInfo && authInfo.realm) || null,
      });
    });
    // DOM probe: capture the field layout of every Microsoft login page
    // step (account chooser, email, password, MFA) as it loads.
    wc.on('dom-ready', function () { probeLoginPage(wc); });
    wc.on('did-finish-load', function () { probeLoginPage(wc); });
  } catch (e) {
    line('hook-error', { error: String((e && e.stack) || e).slice(0, 500) });
  }
}

function hookIpc() {
  try {
    const origOn = ipcMain.on.bind(ipcMain);
    ipcMain.on = function (channel, listener) {
      line('ipc-register', { channel: channel });
      return origOn(channel, function (event) {
        const args = Array.prototype.slice.call(arguments, 1);
        line('ipc', {
          channel: channel,
          sender: (event && event.sender && event.sender.id) || null,
          args: args,
        });
        return listener.apply(this, arguments);
      });
    };
  } catch (e) {
    line('ipc-hook-error', { error: String((e && e.message) || e) });
  }
}

function init() {
  if (inited || !enabled) return;
  inited = true;
  const logPath = process.env.WORKEE_MONITOR_LOG || path.join(os.tmpdir(), 'workee-monitor.log');
  try {
    stream = fs.createWriteStream(logPath, { flags: 'a' });
  } catch (e) {
    console.error('[monitor] cannot open log ' + logPath + ': ' + e.message);
    return;
  }
  stream.on('error', function (e) { console.error('[monitor] log stream error: ' + e.message); });
  line('monitor-start', {
    version: (app.getVersion ? app.getVersion() : null),
    platform: process.platform,
    display: process.env.DISPLAY || null,
    wayland: process.env.WAYLAND_DISPLAY || null,
    gdk: process.env.GDK_BACKEND || null,
    pid: process.pid,
  });
  app.on('web-contents-created', function (_e, wc) {
    let type = null;
    try { type = wc.getType ? wc.getType() : null; } catch (e) {}
    line('web-contents-created', { id: wc.id, type: type });
    hookWebContents(wc);
  });
  app.on('ready', function () { line('app-ready', {}); });
  app.on('window-all-closed', function () { line('window-all-closed', {}); });
  hookIpc();
  line('monitor-ready', { log: logPath });
}

module.exports = { init: init, line: line };
