# Workee AVD Dashboard

A native Electron desktop shell for Windows 365 / Azure Virtual Desktop
Cloud PCs. Opens the real AVD web dashboard in a single window and keeps VM
sessions as in-window tabs instead of spawning new browser windows.

## Why Electron

The AVD web client (`windows.cloud.microsoft/webclient`) is a WASM + Service
Worker app that requires a full Chromium engine. Tauri on Linux uses WebKitGTK,
which cannot load the web client's WASM (blocked by `frame-ancestors` /
access-control checks), so Electron (Chromium) is used instead.

## Running

```bash
python launch_electron.py
```

or directly:

```bash
cd avd-electron && npm install && npx electron .
```

First launch asks you to sign in to Microsoft. Electron's Chromium persists the
session cookies, so subsequent launches are already authenticated.

## How it works

- `main.js` opens a single `BrowserWindow` with a custom tab bar (a `BrowserView`)
  and loads `https://windows.cloud.microsoft/#/devices` as the main view.
- `setWindowOpenHandler` intercepts every popup the AVD dashboard tries to open
  (the web client VM sessions) and redirects them into a new in-window tab
  instead of a new OS window.
- Tab switching is wired via a small preload (`preload-tabbar.js`) over IPC.

## Files

- `avd-electron/main.js` — Electron main process (window, tabs, popup capture)
- `avd-electron/preload-tabbar.js` — IPC bridge for tab clicks
- `avd-electron/package.json` / `package-lock.json` — npm metadata
- `launch_electron.py` — convenience launcher
