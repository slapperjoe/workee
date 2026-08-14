# AGENTS.md

Electron-only desktop shell for the AVD (Windows 365) web dashboard. No tests, no lint, no build step, no CI.

## Commands

```bash
python launch_electron.py            # preferred: sets GDK_BACKEND=x11 (needed on Wayland)
cd avd-electron && npm install && npx electron .   # direct; "start" script = "electron ."
```

There is no test/lint/typecheck command. `package.json` has only a `start` script.

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

- `avd-electron/main.js` — main process: window, `BrowserView` tabs, popup capture, media state/IPC
- `avd-electron/preload-tabbar.js` — tab bar renderer: tabs + speaker/mic controls
- `avd-electron/preload-media.js` — `contextBridge` for `media-hook.js` state reports
- `avd-electron/media-hook.js` — main-world `getUserMedia` tracker + screen-as-webcam substitution (injected via `executeJavaScript`)
- `avd-electron/store.js` — tiny JSON settings store (`userData/settings.json`); persists `screenCamSettings` (`{width,height,fps,smooth}`)
- `launch_electron.py` — launcher (sets `GDK_BACKEND=x11`)
- `screenshare/` — standalone WebRTC screen-share: `signaling-server.js` (Node `ws`, runs in the VM, also serves `receiver.html`), `receiver.html` (VM side)
- `avd-electron/screenshare-capture.html` + `preload-screenshare.js` — host side: hidden window does `getDisplayMedia` → WebRTC → signaling
- Root `README.md` is the only doc; `.gitignore` excludes `node_modules/`, generated `app.html`, and browser/auth artifacts.

## Gotchas

- Browser profiles and auth state live in user data, not the repo (see `.gitignore`).
- `launch_electron.py` sets `GDK_BACKEND=x11` (needed on Wayland). **Do NOT force `--ozone-platform=x11` or call `app.disableHardwareAcceleration()`** — on the target setup those caused GPU-process crashes (`exit_code=139`) and then no window at all. The `ozone-platform=wayland` + "Vulkan not compatible" warning is benign; leave it.
- **Do not toggle `addBrowserView`/`removeBrowserView` on tab switches.** Electron's `BrowserView.ownerWindow` setter leaks a `'closed'` listener on the window each re-attach. `layout()` keeps every view attached and hides inactive ones with zero-size bounds instead.
