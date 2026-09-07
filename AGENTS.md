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
- **Wayland screen capture needs the wlr portal** (`xdg-desktop-portal-wlr` + ScreenCast interface). `checkCapture()` runs at startup: probes `desktopCapturer.getSources`, and on Linux/Wayland, if capture is empty and `~/.config/xdg-desktop-portal-wlr/config` is missing, writes `output_name=<primary>` + `chooser_type=none` (output name via `screen.getAllDisplays()[0].label` → `dms randr` → `wlr-randr`) and restarts the portal. Installing the portal backend itself needs sudo — not done silently. The app-level menu bar is removed via `Menu.setApplicationMenu(null)`.
- **"Screen as webcam" = `getUserMedia` interception, not a real device.** The locked-down VM can't run a receiver/Node, so the working path is: `media-hook.js` wraps `getUserMedia` and, when `screenCam` is on, returns the screen (`getDisplayMedia`) instead of the camera. The AVD client then redirects that "camera" into the VM via its normal webcam redirection. Toggled via `window.__workeeSetScreenCam(...)` from main. Needs nothing running in the VM and no host networking.
- **WebRTC screen-share is dormant/optional** (`screenshare/` + `screenshare-capture.html`). It requires the `signaling-server.js` to run somewhere reachable by *both* host and VM — impossible on a locked-down VM with no shell. Its tab-bar button was removed; the `main.js` wiring (`startScreenShare`/`stopScreenShare`/`toggle-screenshare`) is left in place so it's a one-line re-add if a controllable VM ever appears. Don't treat it as the primary path.

## Layout

- `main.js` — main process: window, `BrowserView` tabs, popup capture, media state/IPC
- `monitor.js` — observe-only event logger (off unless `WORKEE_MONITOR=1`; log path `WORKEE_MONITOR_LOG`, default `$TMPDIR/workee-monitor.log`). Wraps `setWindowOpenHandler` so every popup request + decision is logged; logs navigations, titles, `login` (native auth) events, IPC, console. Debugging aid, no behavior changes.
- `credentials.js` — encrypted credential store (`userData/credentials.enc`). Prefers Electron `safeStorage` when the OS keyring is usable; otherwise scrypt(host+user NID) → AES-256-GCM, 0600. On this box `safeStorage` reports `basic_text` (plaintext) so the fallback is what's active.
- `autofill.js` — auto-fill for the MSAL "device service" credential window (opens as a real second `BrowserWindow` when a Cloud PC session needs interactive re-auth; detected by `type==='window'` + `login.microsoftonline.com`). Clicks the work/school tile, fills the password (native setter + input events), submits, watches the VM tab and clicks its "Reconnect" button after the window closes. Also clicks the in-tab "Sign In" on the "Sign in to Cloud PC" interstitial if it lingers 30s. MFA step is always manual (logged).
- `credentials.html` + `preload-credentials.js` — "VM sign-in credentials…" dialog (email + password, Save/Clear) from the ⋮ app menu; "Auto-fill VM credentials" toggle in the same menu.
- `preload-tabbar.js` — tab bar renderer: tabs + speaker/mic controls
- `preload-media.js` — `contextBridge` for `media-hook.js` state reports
- `media-hook.js` — main-world `getUserMedia` tracker + screen-as-webcam substitution (injected via `executeJavaScript`)
- `store.js` — tiny JSON settings store (`userData/settings.json`); persists `screenCamSettings` (`{width,height,fps,smooth}`)
- `launch_electron.py` — launcher (sets `GDK_BACKEND=x11`)
- `install.sh` — build + install to system (copies to `--install-dir`, symlinks to `--bin-dir`, writes `.desktop` to `--desktop-dir`)
- `screenshare/` — standalone WebRTC screen-share: `signaling-server.js` (Node `ws`, runs in the VM, also serves `receiver.html`), `receiver.html` (VM side)
- `screenshare-capture.html` + `preload-screenshare.js` — host side: hidden window does `getDisplayMedia` → WebRTC → signaling
- Root `README.md` is the only doc; `.gitignore` excludes `node_modules/`, generated `app.html`, and browser/auth artifacts.

## Gotchas

- **Never run two instances on the same profile.** Profile-lock contention (LevelDB `LOCK` in `~/.config/Workee/IndexedDB/`) makes the RDP WASM core fail (`Internal error opening backing store for indexedDB.open` → `RuntimeError: unreachable` in `librdphtml.wasm` → white VM tab; `Could not open the quota database, resetting` in stdout). `main.js` has a `requestSingleInstanceLock()` guard — don't remove it. When killing stragglers, match the FULL command line (a `--remote-debugging-port` launch won't match `electron \.`).
- **VM re-auth opens a real second `BrowserWindow`.** When conditional access expires the device-service token (AADSTS70044), the webclient tab gets `#error=interaction_required` and MSAL calls `window.open(about:blank)` (frameName `msal.<client-id>.ms-device-service://…`); the handler allows it (only http URLs become tabs). That window's page: account chooser = two `role=button` tiles (no form), then password page (visible input hydrates after load), then optional MFA (manual). `autofill.js` keys off `getType()==='window'` + `login.microsoftonline.com` so the dashboard's first-run sign-in (a BrowserView) is never touched.
- **`safeStorage` is `basic_text` (plaintext) on this box.** `credentials.js` therefore falls back to scrypt(host+user) → AES-256-GCM; don't "fix" it to require `safeStorage`.

- Browser profiles and auth state live in user data, not the repo (see `.gitignore`).
- `launch_electron.py` sets `GDK_BACKEND=x11` (needed on Wayland). **Do NOT force `--ozone-platform=x11` or call `app.disableHardwareAcceleration()`** — on the target setup those caused GPU-process crashes (`exit_code=139`) and then no window at all. The `ozone-platform=wayland` + "Vulkan not compatible" warning is benign; leave it.
- **Do not toggle `addBrowserView`/`removeBrowserView` on tab switches.** Electron's `BrowserView.ownerWindow` setter leaks a `'closed'` listener on the window each re-attach. `layout()` keeps every view attached and hides inactive ones with zero-size bounds instead.
