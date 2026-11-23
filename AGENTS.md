# AGENTS.md

Electron-only desktop shell for the AVD (Windows 365) web dashboard. No tests, no lint, no CI.

## Commands

```bash
python launch_electron.py            # dev: sets GDK_BACKEND=x11 (needed on Wayland)
npm install && npx electron .        # dev; "start" script = "electron ."
npm run install-app                  # build + install to system (~/.local)
sudo ./install.sh /opt/Workee /usr/local/bin /usr/share/applications  # system-wide install
```

No test/lint/typecheck command. `package.json` scripts: `start`, `build` (electron-packager), `install-app` (install.sh).

## Hard-earned context (do not undo)

- **Do NOT reintroduce Tauri / WebKitGTK / Python.** These were deliberately stripped (commit dd8677c). The AVD web client is WASM + Service Worker and requires full Chromium; WebKitGTK cannot load it. Electron (Chromium) is the only viable engine here.
- **Tab model = `BrowserView`, not `BrowserWindow`.** `main.js` keeps one `BrowserWindow` and renders every tab (including the main dashboard) as a `BrowserView`. VM session popups are captured by `setWindowOpenHandler` and turned into in-window tabs. If you "fix" tabs by spawning new windows, you break the whole point of the app.
- **Auth is implicit.** Electron's Chromium persists Microsoft session cookies; there is no login code. First launch requires a manual sign-in, later launches are authenticated. Don't add auth handling unless asked.
- **Tab bar is an inline HTML string** injected into a `BrowserView` via `loadURL('data:text/html,...')`; its IPC bridge is `preload-tabbar.js`, which renders tabs + right-side speaker/mic controls from a `state` message pushed by `main.js`. Keep `contextIsolation: true` / `nodeIntegration: false`.
- **Mic/webcam state needs a main-world hook.** `contextIsolation` prevents preloads from patching the page, so `media-hook.js` is injected into each tab via `webContents.executeJavaScript` (main world). It wraps `navigator.mediaDevices.getUserMedia` and reports live audio/video tracks through `preload-media.js` (`window.__workee.report`). Mic mute is applied from main via `executeJavaScript('window.__workeeSetMic(...)')`.
- **Electron media limits (do not chase):** there is no per-tab volume API (only `setAudioMuted`) and no media *device-switching* API (`setDevicePermissionHandler` is HID/serial/USB only). Speaker active state = `audio-state-changed` (`audible`); mic active = live audio track from the hook.
- **Screen capture uses `setDisplayMediaRequestHandler`** (modern API), not the old `chromeMediaSource: 'desktop'` hack — that's deprecated/removed. `main.js` grants a screen via `desktopCapturer`.
- **Wayland screen capture needs the wlr portal** (`xdg-desktop-portal-wlr` + ScreenCast interface) only for the dormant WebRTC screen-share's `desktopCapturer.getSources`. The startup `checkCapture()` probe (which auto-wrote `~/.config/xdg-desktop-portal-wlr/config`) was removed with the screen-cam feature; if that WebRTC path is ever revived and Wayland capture comes back empty, you may need to write that portal config manually (the backend itself needs sudo to install). The app-level menu bar is removed via `Menu.setApplicationMenu(null)`.
- **Screen-cam / "screen as webcam" was REMOVED** (user runs OBS directly and uses its virtual camera instead). It used to be a `getUserMedia` interception: `media-hook.js` wrapped `getUserMedia` and, when `screenCam` was on, returned the screen instead of the camera so the AVD client's webcam redirection showed the host screen. The substitution, its tab-bar toggle, the settings menu, the `screenCamSettings` store key, and the startup capture probe are all gone. `media-hook.js` now wraps `getUserMedia` in a plain pass-through (tracks + reports live audio/video for the tab-bar state) and does NOT substitute any device. Do not re-add the substitution unless asked.
- **The client's multi-monitor path needs three things, not one.** (a) `getScreenDetails` + `screen.isExtended` + a screen position that matches the window — served by `screen-shim.js`. (b) the `innerWidth >= 1600` gate, answered with the doubled width. (c) its **flight flags**: `Nh_Multi_Monitor`, `Nh_Split_Screen` and `Nh_Enable_Windowed_Secondary_Monitor` come back false for this tenant, and the settings UI's display control is disabled by those flags alone, whatever the window looks like. The app rewrites that response in the page (fetch wrapper in `screen-shim.js`), NOT with `protocol.handle('https')` — `net.fetch` inside a protocol handler re-enters the handler for the same request, so every https request fires twice and the client never renders (verified: white screen). Do not "fix" this by moving the rewrite to the network layer.
- **The virtual screen order in `screen-shim.js`/`virtualScreens()` is deliberate.** The settings control is `disabled:!U` where `U` is `MultiMonitor.firstMonitorIndex` = `Hn(window)`, the screen whose position matches the window; with one display that is always 0, so it can never be enabled. The main window is therefore reported as index 1 and the split window as index 0. Monitor geometry is unaffected — each monitor is sized from its own window's canvas rect.
- **If the Chrome UA override is ever used, keep it off the login hosts.** With a Chrome-looking UA the sign-in takes the WebAuthn/FIDO path (`login.microsoft.com/common/fido/get`), and Electron has no native passkey UI — the sign-in stalls on a blank page and the login window never opens. `main.js` sets the session UA for the client's platform check and hands `login.microsoft*` requests back Electron's real UA in `onBeforeSendHeaders`.
- **WebRTC screen-share is dormant/optional** (`screenshare/` + `screenshare-capture.html`). It requires the `signaling-server.js` to run somewhere reachable by *both* host and VM — impossible on a locked-down VM with no shell. Its tab-bar button was removed; the `main.js` wiring (`startScreenShare`/`stopScreenShare`/`toggle-screenshare`) is left in place so it's a one-line re-add if a controllable VM ever appears. Don't treat it as the primary path.

## Layout

- `main.js` — main process: window, `BrowserView` tabs, popup capture, media state/IPC
- `monitor.js` — observe-only event logger (off unless `WORKEE_MONITOR=1`; log path `WORKEE_MONITOR_LOG`, default `$TMPDIR/workee-monitor.log`). Wraps `setWindowOpenHandler` so every popup request + decision is logged; logs navigations, titles, `login` (native auth) events, IPC, console. Debugging aid, no behavior changes.
- `credentials.js` — encrypted credential store (`userData/credentials.enc`). Prefers Electron `safeStorage` when the OS keyring is usable; otherwise scrypt(host+user NID) → AES-256-GCM, 0600. On this box `safeStorage` reports `basic_text` (plaintext) so the fallback is what's active.
- `autofill.js` — auto-fill for the MSAL "device service" credential window (opens as a real second `BrowserWindow` when a Cloud PC session needs interactive re-auth; detected by `type==='window'` + `login.microsoftonline.com`). Clicks the work/school tile, fills the password (native setter + input events), submits, watches the VM tab and clicks its "Reconnect" button after the window closes. Also clicks the in-tab "Sign In" on the "Sign in to Cloud PC" interstitial if it lingers 30s, the post-MFA "verify your account" prompt via its "Maybe later" link, and the SAS "verify it's me" step (`/common/SAS/ProcessAuth`) via its "Not now" link (URL-detected, client-rendered). MFA code entry is always manual (logged).
- `credentials.html` + `preload-credentials.js` — "VM sign-in credentials…" dialog (email + password, Save/Clear) from the ⋮ app menu; "Auto-fill VM credentials" toggle in the same menu.
- `preload-tabbar.js` — tab bar renderer: tabs + speaker/mic controls
- `preload-media.js` — `contextBridge` for `media-hook.js` state reports
- `media-hook.js` — main-world `getUserMedia` tracker (injected via `executeJavaScript`); pass-through only, no device substitution
- `store.js` — tiny JSON settings store (`userData/settings.json`)
- `launch_electron.py` — launcher (sets `GDK_BACKEND=x11`)
- `install.sh` — build + install to system (copies to `--install-dir`, symlinks to `--bin-dir`, writes `.desktop` to `--desktop-dir`)
- `screenshare/` — standalone WebRTC screen-share: `signaling-server.js` (Node `ws`, runs in the VM, also serves `receiver.html`), `receiver.html` (VM side)
- `screenshare-capture.html` + `preload-screenshare.js` — host side: hidden window does `getDisplayMedia` → WebRTC → signaling
- Root `README.md` is the only doc; `.gitignore` excludes `node_modules/`, generated `app.html`, and browser/auth artifacts.

## Gotchas

- **Never run two instances on the same profile.** Profile-lock contention (LevelDB `LOCK` in `~/.config/Workee/IndexedDB/`) makes the RDP WASM core fail (`Internal error opening backing store for indexedDB.open` → `RuntimeError: unreachable` in `librdphtml.wasm` → white VM tab; `Could not open the quota database, resetting` in stdout). `main.js` has a `requestSingleInstanceLock()` guard — don't remove it. When killing stragglers, match the FULL command line (a `--remote-debugging-port` launch won't match `electron \.`).
- **VM re-auth opens a real second `BrowserWindow`.** When conditional access expires the device-service token (AADSTS70044), the webclient tab gets `#error=interaction_required` and MSAL calls `window.open(about:blank)` (frameName `msal.<client-id>.ms-device-service://…`); the handler allows it (only http URLs become tabs). That window's page: account chooser = two `role=button` tiles (no form), then password page (visible input hydrates after load), then optional MFA (manual). `autofill.js` keys off `getType()==='window'` + `login.microsoftonline.com` so the dashboard's first-run sign-in (a BrowserView) is never touched.
- **`safeStorage` is `basic_text` (plaintext) on this box.** `credentials.js` therefore falls back to scrypt(host+user) → AES-256-GCM; don't "fix" it to require `safeStorage`.
- **The cross-device QR MFA ("verify on your phone" QR) will never appear in workee — do not chase it.** It is Chrome's *native* passkey UI, rendered outside the page (on Linux it pops out from the top of the browser window; on Windows the in-page rendering is the Windows-Hello path). It is not page DOM, so the autofill engine's probes can never see or click it, and Electron does not ship the native dialog at all. UA / client-hint identity spoofing is irrelevant here (tested and reverted). MFA in workee is always the in-page password + 6-digit Authenticator code flow.
- **`install.sh` skips the build if `dist/Workee-linux-x64` exists** and silently installs the stale asar. After changing app code, `rm -rf dist` before `npm run install-app` (or verify the asar contents). Also: the running instance must be killed before reinstall/relaunch (single-instance lock + profile lock).

- Browser profiles and auth state live in user data, not the repo (see `.gitignore`).
- `launch_electron.py` sets `GDK_BACKEND=x11` (needed on Wayland). **Do NOT force `--ozone-platform=x11` or call `app.disableHardwareAcceleration()`** — on the target setup those caused GPU-process crashes (`exit_code=139`) and then no window at all. The `ozone-platform=wayland` + "Vulkan not compatible" warning is benign; leave it.
- **Do not toggle `addBrowserView`/`removeBrowserView` on tab switches.** Electron's `BrowserView.ownerWindow` setter leaks a `'closed'` listener on the window each re-attach. `layout()` keeps every view attached and hides inactive ones with zero-size bounds instead.
