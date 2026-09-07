// Auto-fill engine for the MSAL "device service" credential window.
//
// Captured flow (see /tmp/workee-monitor-5.log):
//   1. VM webclient tab (BrowserView) loads; a "Sign in to Cloud PC"
//      interstitial may show with a "Sign In" button, and/or MSAL fires
//      window.open(about:blank) on its own a little later.
//   2. The app's window-open handler allows about:blank, so a real second
//      BrowserWindow (type 'window') is created.
//   3. That window navigates to login.microsoftonline.com:
//        a) work/personal account chooser (two role=button tiles, NO form)
//        b) password page (visible password input hydrates AFTER load)
//        c) optional MFA page (code entry — manual only)
//   4. On success the window auto-closes.
//   5. The VM tab may then show "Disconnected" with a Reconnect button.
//
// This module only ever acts on webContents of type 'window' at
// login.microsoftonline.com, so the dashboard's own first-run sign-in
// (which happens inside a BrowserView) is never touched.
'use strict';

const timers = new Map(); // wcId -> state

let cfg = null;

function log(m) {
  console.log('[autofill] ' + m);
}

function init(options) {
  cfg = {
    enabled: options.enabled || function () { return false; },
    getPassword: options.getPassword || function () { return null; },
    getEmail: options.getEmail || function () { return ''; },
    getFallbackOpener: options.getFallbackOpener || function () { return null; },
  };
}

// ---------- page probes (run in the main world of the login window) ----------

// Detection only; returns JSON. Actions run as separate snippets so each
// executeJavaScript stays small and the log stays readable.
const POLL_JS = '(function () {' +
  '  var vis = function (el) { return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)); };' +
  '  var body = document.body ? document.body.innerText : "";' +
  '  var flat = body.replace(/\\s+/g, " ");' +
  '  var pwdEls = Array.prototype.slice.call(document.querySelectorAll(\'input[type="password"]\')).filter(vis);' +
  '  var btns = Array.prototype.slice.call(document.querySelectorAll("button,[role=button]"));' +
  '  var work = btns.some(function (b) { return vis(b) && /work or school/i.test(b.innerText || ""); });' +
  '  var page = "unknown";' +
  '  if (/verify your identity|two[- ]step verification|enter the (6[- ]digit )?code|authentication code/i.test(flat)) page = "mfa";' +
  '  else if (pwdEls.length) page = "password";' +
  '  else if (work) page = "chooser";' +
  '  return JSON.stringify({' +
  '    page: page,' +
  '    hasPasswordField: pwdEls.length > 0,' +
  '    workTile: work,' +
  '    mfa: page === "mfa",' +
  '    title: document.title,' +
  '    body: flat.slice(0, 200)' +
  '  });' +
'})()';

const CLICK_WORK_JS = '(function () {' +
  '  var vis = function (el) { return !!(el && (el.offsetWidth || el.offsetHeight)); };' +
  '  var btns = Array.prototype.slice.call(document.querySelectorAll("button,[role=button]"));' +
  '  for (var i = 0; i < btns.length; i++) {' +
  '    if (vis(btns[i]) && /work or school/i.test(btns[i].innerText || "")) { btns[i].click(); return "clicked"; }' +
  '  }' +
  '  return "no-tile";' +
'})()';

// Fill password (and email if the page asks for one), then submit.
// Uses the native value setter + input events so SPA frameworks see it.
function fillFn(pwd, email) {
  var vis = function (el) { return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)); };
  var pwdEl = null;
  var emailEl = null;
  Array.prototype.slice.call(document.querySelectorAll('input')).forEach(function (i) {
    if (!vis(i)) return;
    if (i.type === 'password' && !pwdEl) pwdEl = i;
    else if ((i.type === 'email' || i.type === 'text') && !emailEl) emailEl = i;
  });
  if (!pwdEl) {
    return { acted: null, reason: 'no-visible-password-field' };
  }
  var setVal = function (el, val) {
    var proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  var filledEmail = false;
  if (email && emailEl && !emailEl.value) { setVal(emailEl, email); filledEmail = true; }
  setVal(pwdEl, pwd);
  try { pwdEl.focus(); } catch (e) {}
  var btn = null;
  Array.prototype.slice.call(document.querySelectorAll('button,[role=button]')).forEach(function (b) {
    if (!vis(b) || btn) return;
    var t = (b.innerText || '').trim().toLowerCase();
    if (t === 'sign in' || t === 'sign-in' || t === 'next' || t === 'continue' || t === 'submit') btn = b;
  });
  var done = false;
  if (btn) { btn.click(); done = true; }
  else {
    var f = pwdEl.closest ? pwdEl.closest('form') : null;
    if (f) { if (f.requestSubmit) f.requestSubmit(); else f.submit(); done = true; }
  }
  return { acted: done ? 'filled-and-submitted' : 'filled-only', filledEmail: filledEmail };
}

const MFA_JS = '(function () {' +
  '  var vis = function (el) { return !!(el && (el.offsetWidth || el.offsetHeight)); };' +
  '  var out = { title: document.title, url: location.href };' +
  '  out.inputs = Array.prototype.slice.call(document.querySelectorAll("input")).filter(vis).map(function (i) {' +
  '    return { type: i.type, name: i.name, id: i.id, aria: i.getAttribute("aria-label"), ph: i.placeholder };' +
  '  });' +
  '  out.buttons = Array.prototype.slice.call(document.querySelectorAll("button,[role=button]")).filter(vis).map(function (b) {' +
  '    return (b.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 50);' +
  '  }).filter(Boolean);' +
  '  out.links = Array.prototype.slice.call(document.querySelectorAll("a")).filter(vis).map(function (a) {' +
  '    return (a.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 50);' +
  '  }).filter(Boolean);' +
  '  var body = document.body ? document.body.innerText.replace(/\\s+/g, " ") : "";' +
  '  out.body = body.slice(0, 300);' +
  '  var err = document.querySelector("[role=alert], .ms-Alert, .verifier-error, .error-message, .text-error");' +
  '  out.errorText = err ? (err.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 200) : null;' +
  '  return JSON.stringify(out);' +
'})()';

// ---------- VM-tab probes (interstitial + reconnect) ----------

const INTERSTITIAL_JS = '(function () {' +
  '  var flat = (document.body ? document.body.innerText : "").replace(/\\s+/g, " ");' +
  '  var showing = /sign in to cloud pc/i.test(flat) && /please grant permission/i.test(flat);' +
  '  var vis = function (el) { return !!(el && (el.offsetWidth || el.offsetHeight)); };' +
  '  var btns = Array.prototype.slice.call(document.querySelectorAll("button,[role=button]"));' +
  '  var hasSignin = btns.some(function (b) { return vis(b) && (b.innerText || "").trim().toLowerCase() === "sign in"; });' +
  '  return JSON.stringify({ showing: showing, hasSignin: hasSignin });' +
'})()';

const INTERSTITIAL_CLICK_JS = '(function () {' +
  '  var vis = function (el) { return !!(el && (el.offsetWidth || el.offsetHeight)); };' +
  '  var btns = Array.prototype.slice.call(document.querySelectorAll("button,[role=button]"));' +
  '  for (var i = 0; i < btns.length; i++) {' +
  '    if (vis(btns[i]) && (btns[i].innerText || "").trim().toLowerCase() === "sign in") { btns[i].click(); return "clicked"; }' +
  '  }' +
  '  return "no-button";' +
'})()';

const RECONNECT_JS = '(function () {' +
  '  var vis = function (el) { return !!(el && (el.offsetWidth || el.offsetHeight)); };' +
  '  var btns = Array.prototype.slice.call(document.querySelectorAll("button,[role=button]"));' +
  '  for (var i = 0; i < btns.length; i++) {' +
  '    if (vis(btns[i]) && /reconnect/i.test(btns[i].innerText || "")) { btns[i].click(); return JSON.stringify({ acted: "reconnect-clicked" }); }' +
  '  }' +
  '  var body = document.body ? document.body.innerText.replace(/\\s+/g, " ").slice(0, 200) : "";' +
  '  return JSON.stringify({ acted: null, body: body });' +
'})()';

// ---------- engine ----------

function isMsLoginUrl(u) {
  return !!u && u.indexOf('login.microsoftonline.com') >= 0;
}

function onWebContents(wc) {
  try {
    wc.on('did-navigate', function (_e, url) { tryStart(wc, url); });
    wc.on('did-frame-navigate', function (_e, url, _p, _r, main) { if (main) tryStart(wc, url); });
    wc.on('destroyed', function () { onWindowGone(wc); });
    wc.on('render-process-gone', function () { onWindowGone(wc); });
  } catch (e) {
    log('hook error: ' + ((e && e.message) || e));
  }
}

function getOpener(wc) {
  try {
    const o = wc.getOpener ? wc.getOpener() : null;
    if (o && !o.isDestroyed()) return o;
  } catch (e) {}
  return null;
}

function hasActiveWindow() {
  return timers.size > 0;
}

function tryStart(wc, url) {
  if (!cfg || !cfg.enabled()) return;
  if (timers.has(wc.id)) return;
  let type = null;
  try { type = wc.getType ? wc.getType() : null; } catch (e) {}
  // Hard gate: only the MSAL popup window (real BrowserWindow). The
  // dashboard's first-run sign-in happens inside a BrowserView and is
  // deliberately left to the user (manual MFA).
  if (type !== 'window' || !isMsLoginUrl(url)) return;
  const opener = getOpener(wc) || cfg.getFallbackOpener();
  timers.set(wc.id, {
    stage: 'watch',
    deadline: Date.now() + 180000,
    opener: opener,
    mfaNotified: false,
    mfaCaptured: false,
    fillHinted: false,
  });
  log('MSAL credential window detected (opener: ' + (opener ? 'yes' : 'no') + ') — auto-fill armed');
  const timer = setInterval(function () { poll(wc); }, 700);
  timers.get(wc.id).timer = timer;
  poll(wc);
}

async function poll(wc) {
  const st = timers.get(wc.id);
  if (!st || !cfg) return;
  if (wc.isDestroyed()) return;
  if (Date.now() > st.deadline) {
    log('giving up (deadline) — stage: ' + st.stage);
    stop(wc);
    return;
  }
  let u = '';
  try { u = wc.getURL() || ''; } catch (e) { return; }
  if (u && u !== 'about:blank' && !isMsLoginUrl(u)) {
    // Left the login flow (post-auth redirect or user action).
    stop(wc);
    return;
  }
  let r = null;
  try {
    const out = await wc.executeJavaScript(POLL_JS, true);
    try { r = JSON.parse(out); } catch (e) { return; }
  } catch (e) {
    return; // webContents gone
  }
  if (!r) return;

  if (r.mfa) {
    if (!st.mfaNotified) {
      st.mfaNotified = true;
      st.stage = 'mfa';
      log('MFA / verification step detected — enter the code in the credential window manually');
      if (!st.mfaCaptured) {
        st.mfaCaptured = true;
        wc.executeJavaScript(MFA_JS, true).then(function (out) {
          try { log('mfa-page: ' + out); } catch (e) {}
        }).catch(function () {});
      }
    }
    return; // keep polling: after MFA the window closes on its own
  }

  if (r.page === 'chooser') {
    st.stage = 'chooser';
    if (r.workTile) {
      try {
        const res = await wc.executeJavaScript(CLICK_WORK_JS, true);
        log('work/school tile: ' + res);
      } catch (e) { return; }
      st.stage = 'password-wait';
    }
    return;
  }

  if (r.page === 'password') {
    if (st.stage === 'submitted' && Date.now() - st.submittedAt < 5000) return;
    const pwd = cfg.getPassword();
    if (!pwd) {
      if (!st.fillHinted) {
        st.fillHinted = true;
        log('password page shown but no stored password — enter it manually (Settings > Set credentials… to enable auto-fill)');
      }
      st.stage = 'manual';
      return;
    }
    const email = cfg.getEmail();
    log('password page ready — filling and submitting');
    // Set the stage BEFORE the await: otherwise a 700ms poll can fire a
    // second fill/submit while the first is in flight, and a double
    // submit can race the MFA handoff and invalidate the flow.
    st.stage = 'submitted';
    st.submittedAt = Date.now();
    try {
      const out = await wc.executeJavaScript('(' + fillFn.toString() + ')(' + JSON.stringify(pwd) + ', ' + JSON.stringify(email) + ')', true);
      try { log('fill result: ' + JSON.stringify(out)); } catch (e) {}
      if (out && (out.acted === 'filled-only' || /filled-only/.test(String(out)))) {
        log('submitted via form; if nothing happens, click Sign in in the credential window');
      }
    } catch (e) {
      log('fill error: ' + ((e && e.message) || e));
    }
    return;
  }

  if (r.page === 'unknown' && st.stage === 'watch') {
    // Page still hydrating; log once so the user knows we're waiting.
    st.stage = 'hydrating';
    log('waiting for login page to hydrate (' + (r.body || '').slice(0, 80) + ')');
  }
}

function onWindowGone(wc) {
  const st = timers.get(wc.id);
  if (!st) return;
  if (st.timer) clearInterval(st.timer);
  timers.delete(wc.id);
  const wasActive = ['chooser', 'password-wait', 'submitted', 'mfa', 'manual', 'hydrating'].indexOf(st.stage) >= 0;
  log('credential window closed (was at stage: ' + st.stage + (st.stage === 'submitted' ? ', submitted ' + Math.round((Date.now() - st.submittedAt) / 1000) + 's ago' : '') + ')');
  if (wasActive) scheduleReconnect(st.opener);
}

function stop(wc) {
  const st = timers.get(wc.id);
  if (!st) return;
  if (st.timer) clearInterval(st.timer);
  timers.delete(wc.id);
  log('auto-fill disengaged (stage: ' + st.stage + ')');
  if (st.stage === 'submitted' || st.stage === 'mfa') scheduleReconnect(st.opener);
}

// After the credential window goes away, the VM tab may need a manual
// Reconnect click. Poll the opener (VM webclient tab) for up to ~2 min.
function scheduleReconnect(opener) {
  let target = opener;
  if (!target || (typeof target.isDestroyed === 'function' && target.isDestroyed())) {
    target = cfg.getFallbackOpener ? cfg.getFallbackOpener() : null;
  }
  if (!target) {
    log('no VM tab to reconnect — if the session shows "Disconnected", click Reconnect manually');
    return;
  }
  log('watching VM tab for reconnect state (up to 2 min)');
  let attempts = 0;
  const timer = setInterval(function () {
    attempts++;
    if (attempts > 40) {
      clearInterval(timer);
      log('reconnect watch ended — session did not report state; check the VM tab');
      return;
    }
    if (typeof target.isDestroyed === 'function' && target.isDestroyed()) {
      clearInterval(timer);
      return;
    }
    target.executeJavaScript(RECONNECT_JS, true).then(function (out) {
      let r = null;
      try { r = JSON.parse(out); } catch (e) { return; }
      if (r.acted) {
        clearInterval(timer);
        log('clicked Reconnect on the VM tab — session should resume');
        return;
      }
      if (r.body && !/disconnected/i.test(r.body)) {
        clearInterval(timer);
        log('VM tab no longer shows "Disconnected" — session is back, no action needed');
      }
    }).catch(function () {
      clearInterval(timer);
    });
  }, 3000);
}

// Called for every webclient tab: if the "Sign in to Cloud PC"
// interstitial is still showing after 30s and no MSAL window has opened,
// click its in-tab "Sign In" button to kick off the credential flow.
function scheduleInterstitialCheck(wc) {
  setTimeout(function () {
    if (!wc || typeof wc.isDestroyed === 'function' || wc.isDestroyed()) return;
    wc.executeJavaScript(INTERSTITIAL_JS, true).then(function (out) {
      let r = null;
      try { r = JSON.parse(out); } catch (e) { return; }
      if (!r || !r.showing || !r.hasSignin) return;
      if (hasActiveWindow()) {
        log('Cloud PC interstitial showing but a credential window is already active — leaving it');
        return;
      }
      log('Cloud PC permission interstitial still showing after 30s — clicking in-tab "Sign In"');
      wc.executeJavaScript(INTERSTITIAL_CLICK_JS, true).then(function (res) {
        log('interstitial click: ' + res);
      }).catch(function () {});
    }).catch(function () {});
  }, 30000);
}

module.exports = { init, onWebContents, scheduleInterstitialCheck, hasActiveWindow };
