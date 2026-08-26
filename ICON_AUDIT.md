# Workee icon audit

Scope: every application icon asset in the repo and every place the icon is
referenced, so the icon can be replaced with a Microsoft-like mark (see
t_fa1a50c7 for the new SVG master) and rebuilt/verified (t_d12616ee).

App: **Electron (Chromium) desktop shell, Linux x64 only.** Packaged with
`@electron/packager` (no Electron Forge/maker, no `.app`/`.exe`/`.icns`/`.ico`
toolchain). There is no macOS or Windows target and no Android/manifest.
No Tauri/WebKitGTK (stripped in dd8677c).

## 1. Icon assets in the repository

There is exactly **one** icon asset tracked in git:

| Path                    | Format | Size    | Colors              | Notes |
|-------------------------|--------|---------|---------------------|-------|
| `avd-electron/icon.png` | PNG    | 256×256 | 8-bit RGBA (colortype 6), non-interlaced | A blue-gradient glyph on a transparent background (~15.5% transparent, 8px margin on each edge). **Not** a Microsoft logo. |

That file is the single source for every other icon artifact — `install.sh`
copies it (without resizing) to each installed location. There are **no**
`.ico`, `.icns`, `.svg`, multi-size PNG sets, `build/` icon dir, or `resources/`
icon in the repo. `avd-electron/resources/` and `avd-electron/build/` do not
exist (both are gitignored).

### Non-icon SVGs (do not confuse with the app icon)
`avd-electron/preload-tabbar.js` defines inline SVG glyphs for the tab bar
(`SPK`, `MIC`, `CAM`, `X`, `GEAR`, `KA`, `DOTS`) and `main.js` inlines the same
style in the tab-bar HTML. These are UI control icons in the tab bar, not the
application icon — leave them alone. `screenshare/receiver.html` /
`screenshare-capture.html` use only CSS `background:` colors, no icon files.

## 2. Where the icon is referenced (what needs updating)

### Build / packaging
| Ref | Location | What it does | On Linux |
|-----|----------|--------------|----------|
| `--icon=icon.png` | `avd-electron/package.json:10` (script `build`) | Passed to electron-packager | **No-op.** Packager only embeds the icon on Windows (`.ico`) and macOS (`.icns`); on Linux the ELF gets no icon. |
| `--icon=icon.png` | `avd-electron/install.sh:12` | Same packager invocation used by `install-app` | No-op (same reason). |

> Verified: the built app `avd-electron/dist/Workee-linux-x64/resources/`
> contains only `app.asar` — no embedded `resources/icon.png`. The `icon.png`
> is still copied into the asar (it lives in the packaged dir) but nothing in
> the running app reads it.

### install.sh — what the launcher actually renders
| Ref | Location | What it does |
|-----|----------|--------------|
| `cp icon.png "$INSTALL_DIR/resources/icon.png"` | `install.sh:31-34` | Copies the source icon into the install tree. Vestigial for the launcher (the `.desktop` uses `Icon=workee` by name, not this path) but it is a copy of the icon that must become the new one. |
| `install_icon_sizes=(16 32 48 64 128 256 512)` | `install.sh:60` | The set of hicolor sizes the launcher is given. |
| `cp icon.png "$dest_dir/${APP_ID}.png"` | `install.sh:59-72` | Copies the **same source PNG** (no resize) into `$ICON_ROOT/hicolor/{16,32,48,64,128,256}x*/apps/workee.png` and into `hicolor/scalable/apps/workee.png` (for 512). **This is what the desktop launcher/menu actually displays.** |
| `index.theme` generation | `install.sh:74-110` | Writes `$ICON_ROOT/hicolor/index.theme` so the `hicolor` dir is a valid theme (Qt/GTK/QuickShell need it, else `Icon=workee` → missing-icon placeholder). Not icon pixels, but it depends on the size dirs above. |
| `.desktop` `Icon=workee` | `install.sh:120` | The generated `$DESKTOP_DIR/workee.desktop` points at the icon **by theme name** `workee`. Must keep matching the hicolor filename `workee.png`. |

### Running app (in-window / taskbar icon)
| Ref | Location | Gap |
|-----|----------|-----|
| `new BrowserWindow({ width, height, minWidth, minHeight, title })` | `main.js:374-380` | **No `icon:` option and no `setIcon()` anywhere in `main.js`.** The main window (and therefore the taskbar/compositor window icon) uses the **default Electron icon**, not the app icon. This is the one place the app icon is *not* wired up. To make the new icon appear in the window/taskbar, add `icon: path.join(__dirname, 'icon.png')` to the `BrowserWindow` options. |

### Runtime-installed artifacts (produced by install.sh, not in the repo)
- `$INSTALL_DIR/resources/icon.png` (single-size copy)
- `$ICON_ROOT/hicolor/{16,32,48,64,128,256}x*/apps/workee.png` + `hicolor/scalable/apps/workee.png`
- `$ICON_ROOT/hicolor/index.theme`
- `$DESKTOP_DIR/workee.desktop` → `Icon=workee`

All of these are regenerated from the repo `icon.png` on each install, so
replacing `avd-electron/icon.png` + re-running `install.sh` updates them.

## 3. Required formats & sizes per target platform

**Linux x64 (the only target):**
- Format: **PNG** (RGBA). No `.ico`/`.icns` needed.
- Launcher (hicolor theme) consumes these sizes, as configured in
  `install.sh:60`: **16, 32, 48, 64, 128, 256** (fixed `NxN/apps/`) and
  **512** (in `scalable/apps/`).
- `resources/icon.png` copy: one size (make it 256 or 512).
- `--icon=icon.png` packager arg: keep pointing at the PNG (no-op on Linux,
  but correct if a macOS/Windows build is ever added).
- In-window/taskbar: needs a `BrowserWindow({ icon })` (see gap above).

> Current `install.sh` does a plain `cp` (no resize), so every hicolor slot
> holds a copy of the one source PNG and the launcher down-scales it. That
> works, but small sizes (16/32/48) are sharper if pre-rendered. **Optional
> quality improvement for t_d12616ee:** use the new 1024×1024 master and add a
> resize step (e.g. ImageMagick/`pnmtopng`) in `install.sh` to emit true
> per-size PNGs. Not required for a correct install.

**macOS (NOT a target):** would need a `.icns` (multi-size 16–1024) and a
`--platform=darwin` packager run. Not required for this task.

**Windows (NOT a target):** would need a `.ico` (16–256 multi-size). Not
required for this task.

## 4. Bottom line for the icon replacement

1. Replace `avd-electron/icon.png` with the new Microsoft-like icon. Use the
   1024×1024 PNG from t_fa1a50c7 (or a 512×512 down-scale to keep the file
   small). Keep the RGBA/transparent format so it sits well on any theme.
2. Re-run `install.sh` (rebuild + reinstall) — it regenerates `resources/`,
   the hicolor set, `index.theme`, and the `.desktop` from the new icon.
3. **Also add `icon:` to the `BrowserWindow` in `main.js`** if the new icon
   should appear in the window/taskbar (currently the default Electron icon
   shows there — the one reference the prior fix didn't cover).
4. Optionally pre-render per-size hicolor PNGs in `install.sh` for crisper
   small icons.