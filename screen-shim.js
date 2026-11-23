// Main-world shim for the "one window per monitor" path.
//
// The AVD client only enters its real multi-monitor code when three things hold:
//   window.getScreenDetails() returns >= 2 screens,   (Electron has no Window Management API)
//   window.screen.isExtended is true,                 (single display => false)
//   window.screen position matches one of those screens (Hn() maps a window to a screen index)
//
// So we serve a fake two-screen world: one virtual screen per workee window. Each
// window gets its own fixed entry, so moving or resizing a window does not break the
// screen->window mapping the client uses, and the client sizes each remote monitor from
// that window's own canvas rect - which is the control the fixed-centre split cannot give.
//
// contextIsolation means a preload cannot patch the page, so main injects the config as
// globals and then executes this file with executeJavaScript.
//
// It also rewrites the client's flight-flag response when main passes __workeeFlightFlags
// (see the fetch wrapper below - a protocol handler cannot do this; it recurses).

(function () {
  var screens = window.__workeeScreens || [];
  var idx = window.__workeeScreenIndex;
  if (!screens.length || idx == null || idx >= screens.length) return false;

  // The full ScreenDetails shape. The display cards read these fields, so a partial object
  // is filtered out - that is the difference between "two screens listed" and an empty
  // display section. Values are taken from the real primary display where they matter
  // (scale factor), and the geometry is the virtual half.
  function asScreen(s, current) {
    return {
      id: s.id || ('workee-virtual-' + s.index),
      deviceScaleFactor: s.deviceScaleFactor || 1,
      maximumWindowWidth: 4096,
      positionAt: 'unknown',
      orientation: 'landscape-primary',
      type: 'display',
      left: s.left,
      top: s.top,
      width: s.width,
      height: s.height,
      availLeft: s.left,
      availTop: s.top,
      availWidth: s.width,
      availHeight: s.height,
      isExtended: screens.length > 1,
      onCurrentScreen: !!current,
      matchesMediaQuery: function () { return false; },
    };
  }

  var mine = asScreen(screens[idx], true);
  try {
    Object.defineProperty(window, 'screen', { value: mine, configurable: true });
  } catch (e) { return false; }

  function getScreenDetails() {
    var d = {
      screens: screens.map(function (s, i) { return asScreen(s, i === idx); }),
      currentScreen: mine,
    };
    // The client attaches 'currentscreenchange' / 'screenschange' listeners; our
    // virtual layout is fixed, so a no-op listener is enough to keep it happy.
    d.addEventListener = function () {};
    d.removeEventListener = function () {};
    return Promise.resolve(d);
  }
  window.getScreenDetails = getScreenDetails;

  // Window position. This is the part of the client's monitor measurement that the app can
  // supply: Qn() builds each monitor as window.screenLeft * devicePixelRatio plus the container
  // rect, and on Wayland a client has no window position at all - two windows both report 0,0
  // (verified: probe21), so the layout came out as two identical monitors at the same origin.
  // Reporting the virtual screen's own origin here makes the layout self-consistent. The size
  // half is still the real window, because the container rect is the actual DOM rect - that is
  // the control you get by sizing each window yourself.
  ['screenLeft', 'screenTop', 'screenX', 'screenY'].forEach(function (name) {
    try {
      Object.defineProperty(window, name, {
        configurable: true,
        get: function () { return name.indexOf('Top') >= 0 || name.indexOf('Y') >= 0 ? mine.top : mine.left; },
      });
    } catch (e) {}
  });

  // The split window's prompt is not just a call: the client removes its
  // "click to go fullscreen and continue working" text inside a fullscreenchange handler
  // that checks document.fullscreenElement. A plain object cannot satisfy requestFullscreen's
  // screen option (type conversion fails), and on one physical display there is no
  // fullscreen-to-a-half anyway. So for this document we report a coherent fullscreen state:
  // accept the request, expose fullscreenElement, and fire the event the handler listens for.
  // Geometry is unchanged. Real Screens (this world off) still use the native path.
  if (window.__workeeScreenIndex === 0 && !window.__workeeFsWrap) {
    window.__workeeFsWrap = true;
    var origRequestFullscreen = Element.prototype.requestFullscreen;
    var origExitFullscreen = Document.prototype.exitFullscreen;
    var fakeElement = null;
    function isVirtual(s) {
      return s && Array.isArray(window.__workeeScreens) &&
        window.__workeeScreens.some(function (x) { return x.id === s.id; });
    }
    // Both targets: an event fired on document does not reach a listener registered on the
    // window (verified, probe23), and the client's handler that hides the message is registered
    // on the window while its overlap watchdog is registered on the document. Each listener gets
    // exactly one event, so dispatch on both.
    function fire() {
      try { document.dispatchEvent(new Event('fullscreenchange')); } catch (e) {}
      try { window.dispatchEvent(new Event('fullscreenchange')); } catch (e) {}
    }
    try {
      Object.defineProperty(document, 'fullscreenElement', {
        configurable: true,
        get: function () { return fakeElement; },
      });
    } catch (e) { return false; }
    Element.prototype.requestFullscreen = function (options) {
      var s = options && options.screen;
      if (isVirtual(s)) {
        console.log('[shim] requestFullscreen on virtual screen ' + s.id + ' - accepted as fullscreen state, geometry unchanged');
        fakeElement = this;
        fire();
        return Promise.resolve();
      }
      var self = this;
      return origRequestFullscreen.call(self, options).then(function (r) {
        fakeElement = r || self;
        fire();
        return r;
      }).catch(function (e) {
        fakeElement = null;
        fire();
        throw e;
      });
    };
    document.exitFullscreen = function () {
      if (!fakeElement) return origExitFullscreen.call(document);
      fakeElement = null;
      fire();
      return Promise.resolve();
    };
  }

  // Gate only. The client's settings UI shows the display options only when
  // window.innerWidth >= 1600, and that constant is inside the bundle, so we cannot
  // change it - we can only answer differently when it is asked. Reporting the combined
  // width of the virtual screens (double for a two-way split) is what the check is
  // really asking: "is there room for two monitors side by side".
  //
  // It has to stay live: the client re-reads innerWidth on resize, so a value captured
  // at inject time would go stale. We keep the original accessor and multiply its result.
  // This does not affect monitor geometry - the layout math uses each window's own canvas
  // rect, so each window's size still drives its own monitor. Only the UI gate sees the
  // doubled number, plus the client's taskbar snap math, which can sit off-centre.
  if (window.__workeeGateMode === 'double') {
    var desc = Object.getOwnPropertyDescriptor(window, 'innerWidth');
    var realWidth = desc && desc.get ? desc.get : (desc && 'value' in desc ? function () { return desc.value; } : null);
    if (realWidth) {
      Object.defineProperty(window, 'innerWidth', {
        configurable: true,
        get: function () { return realWidth() * screens.length; },
      });
    }
  }

  // Flight flags. The settings page reads these from the flight list the client fetches on
  // load, and for this tenant Nh_Multi_Monitor, Nh_Split_Screen and
  // Nh_Enable_Windowed_Secondary_Monitor come back false - which alone disables the
  // multi-monitor control whatever the window looks like. The app cannot set a flight flag,
  // so the only lever is to rewrite that response.
  //
  // A protocol handler would be the natural place, but it recurses: net.fetch inside
  // protocol.handle('https') re-enters the handler for the same request (verified), so every
  // https request in the app fires twice and the client fails to boot - a white screen. The
  // rewrite therefore happens in the page, where it keeps the same request path and cookies.
  // The cost is timing: it only covers requests made after this script is injected, i.e. the
  // boot-time flight call is missed if it happens before the DOM is ready.
  var flags = window.__workeeFlightFlags;
  if (flags && !window.__workeeFlightWrap) {
    window.__workeeFlightWrap = true;
    var origFetch = window.fetch;
    function isFlightBody(body) {
      if (!body || typeof body !== 'object') return false;
      var f = body.features || body;
      for (var k in f) { if (String(k).indexOf('Nh_Multi_Monitor') === 0) return true; }
      return false;
    }
    window.fetch = function (input, init) {
      return origFetch.call(window, input, init).then(function (res) {
        try {
          if (!res.ok) return res;
          var ct = res.headers.get('content-type') || '';
          if (ct.indexOf('json') < 0) return res;
          return res.clone().json().then(function (body) {
            if (!isFlightBody(body)) return res;
            var f = body.features || body;
            Object.keys(flags).forEach(function (k) { f[k] = flags[k]; });
            console.log('[flight] ' + ((typeof input === 'string') ? input : (input && input.url) || '?') +
              ' -> ' + Object.keys(flags).join(', '));
            return new Response(JSON.stringify(body), { status: res.status, headers: res.headers });
          }).catch(function () { return res; });
        } catch (e) { return res; }
      });
    };
  }

  return true;
})();
