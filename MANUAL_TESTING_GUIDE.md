# Manual Resolution Testing Guide — Live VM QA

## Prerequisites

- Working Azure VM infrastructure with a display server (X11 or Wayland)
- i3 or fluxbox window manager installed on the VM
- Browser with the dashboard running (`python dashboard_server.py`)
- At least one VM in each state: running, stopped, and portal (ARM-managed)
- OBS Studio + DistroAV NDI plugin (if testing screen sharing)

## Quick Verification — Automated Tests

Before starting manual tests, confirm the unit/integration suite is green:

```bash
cd /home/mark/code/workee
python -m pytest test_resolution_integration.py test_azure_wrapper.py -v
```

Expected: 182 tests pass (90 integration + 92 unit). These cover the full priority chain, presets, parsing, validation, edge cases, and all integration points.

---

## Part 1 — Common Resolutions (1024×768, 1280×720, 1920×1080, 3840×2160)

### 1.1 Via Dashboard Auto-Detect (viewport reporting)

The dashboard frontend reports `window.innerWidth` × `window.innerHeight` on load and on every browser resize. This is the recommended path — the system should adapt without any manual configuration.

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open the dashboard in Chrome/Firefox | Dashboard loads with VM tiles visible |
| 2 | Open DevTools → set viewport to **1024×768** | Dashboard resizes; viewport POSTed to `/api/resolution` |
| 3 | Click **Connect** on any running VM | Chromium launches at **1024×768** (or nearest feasible) |
| 4 | Verify: the VM's RDP/AVD session fills the window without scrollbars or cut-off elements | No artifacts |
| 5 | Repeat steps 2–4 with viewport sizes: **1280×720**, **1920×1080**, **3840×2160** | Each resolution launches the session at the correct dimensions |
| 6 | For 3840×2160: if your physical monitor is smaller, the Chromium window should still use 3840×2160 internally (you can verify via the Chromium address bar: `javascript:window.innerWidth+'x'+window.innerHeight`) | Reports 3840×2160 |

### 1.2 Via `AZURE_RESOLUTION` Environment Variable

This overrides auto-detect — useful for headless or fixed-resolution setups.

```bash
# Test each preset
AZURE_RESOLUTION=desktop python -c "from azure_wrapper.resolution import get_resolution; print(get_resolution())"
# Expected: {'width': 1920, 'height': 1080}

AZURE_RESOLUTION=hd python -c "from azure_wrapper.resolution import get_resolution; print(get_resolution())"
# Expected: {'width': 1280, 'height': 720}

AZURE_RESOLUTION=4k python -c "from azure_wrapper.resolution import get_resolution; print(get_resolution())"
# Expected: {'width': 3840, 'height': 2160}

# Test exact WxH strings
AZURE_RESOLUTION=1024x768 python -c "from azure_wrapper.resolution import get_resolution; print(get_resolution())"
# Expected: {'width': 1024, 'height': 768}

AZURE_RESOLUTION=3840x2160 python -c "from azure_wrapper.resolution import get_resolution; print(get_resolution())"
# Expected: {'width': 3840, 'height': 2160}
```

Then test with the actual dashboard and VM launchers:

```bash
# Start dashboard with a fixed resolution
AZURE_RESOLUTION=1024x768 python dashboard_server.py
# → Connect to a VM → verify the session opens at 1024×768 regardless of browser viewport

# Test all four common resolutions through a full session launch cycle
for RES in 1024x768 1280x720 1920x1080 3840x2160; do
    echo "=== Testing $RES ==="
    AZURE_RESOLUTION=$RES python avd_client.py --dry-run 2>&1 | grep -i "resolution\|viewport"
done
```

### 1.3 Via `--resolution` CLI Flag

```bash
# AVD client
python avd_client.py --resolution 1024x768
python avd_client.py --resolution 1280x720
python avd_client.py --resolution 1920x1080
python avd_client.py --resolution 3840x2160

# AVD session manager
python avd_session.py --resolution 1024x768
python avd_session.py --resolution hd
python avd_session.py --resolution 4k

# AVD login
python avd_login.py --resolution 1280x720
```

### 1.4 Visual Artifact Check — Each Resolution

For each resolution (1024×768, 1280×720, 1920×1080, 3840×2160):

1. **RDP session**: The remote desktop should fill the Chromium viewport. No black bars, no scrollbars. Text should be readable (not too small at 4K, not blurry at 1024×768).
2. **Window manager (i3/fluxbox)**: Open a terminal inside the VM. Tile a few windows. The WM should arrange them within the viewport — no windows placed off-screen.
3. **Application rendering**: Open a web browser inside the VM. Navigate to a complex page (e.g. GitHub, YouTube). Layout should be consistent; no elements overlapping or misaligned.
4. **OBS NDI (if applicable)**: After running `setup_host_ndi.sh`, the OBS profile should output at the resolved dimensions. Verify via `obs-websocket` or OBS UI → Settings → Video.

---

## Part 2 — Custom User-Configured Resolution

### 2.1 Via dict in code

```python
from azure_wrapper.resolution import get_resolution

# Test unusual aspect ratios
for res in [{"width": 1440, "height": 900},
            {"width": 2560, "height": 1080},   # ultrawide
            {"width": 3440, "height": 1440},   # 21:9
            {"width": 1536, "height": 2048}]:  # portrait
    result = get_resolution(configured=res)
    print(f"{res} → {result}")
    assert result == res
```

### 2.2 Dashboard with custom resolution POST

```bash
# Start the dashboard
python dashboard_server.py &

# POST a custom resolution (simulating a browser with a custom viewport)
curl -s -X POST http://localhost:8080/api/resolution \
  -H "Content-Type: application/json" \
  -d '{"width": 2560, "height": 1080}'

# Verify it was stored
curl -s http://localhost:8080/api/resolution
# Expected: {"width": 2560, "height": 1080, "source": "client_viewport"}
```

Then connect to a VM — the session should launch at 2560×1080.

---

## Part 3 — Edge Cases

### 3.1 Missing Auto-Detect Fallback

When no display server is available:

```bash
# Unset display variables to simulate headless/SSH
unset DISPLAY
unset WAYLAND_DISPLAY

python -c "from azure_wrapper.resolution import get_resolution; print(get_resolution())"
# Expected: {'width': 1920, 'height': 1080}  (the FALLBACK)

# Verify the fallback message in logs
python -c "
import logging; logging.basicConfig(level=logging.DEBUG)
from azure_wrapper.resolution import get_resolution
print(get_resolution())
" 2>&1 | grep -i "fallback\|detect"
# Should show: resolution: using configured ... or resolution: using fallback
```

Re-enable display afterwards: `export DISPLAY=:0` (or your actual display).

### 3.2 Invalid User Overrides

The system must not crash on bad input, and must fall back safely.

```bash
# Garbage string — should fall back
AZURE_RESOLUTION=garbage python -c "from azure_wrapper.resolution import get_resolution; print(get_resolution())"
# Expected: {'width': 1920, 'height': 1080}  (fallback)

# Empty string
AZURE_RESOLUTION="" python -c "from azure_wrapper.resolution import get_resolution; print(get_resolution())"
# Expected: {'width': 1920, 'height': 1080}  (fallback)

# Negative dimensions — clamped to minimum
python -c "
from azure_wrapper.resolution import get_resolution
print(get_resolution(configured={'width': -100, 'height': -50}))
"
# Expected: {'width': 800, 'height': 600}  (clamped to MIN)

# Massive dimensions — clamped to maximum
python -c "
from azure_wrapper.resolution import get_resolution
print(get_resolution(configured={'width': 99999, 'height': 99999}))
"
# Expected: {'width': 7680, 'height': 4320}  (clamped to MAX)

# Zero dimensions — ignored, falls back
python -c "
from azure_wrapper.resolution import get_resolution
print(get_resolution(client_viewport={'width': 0, 'height': 0}))
"
# Expected: {'width': 1920, 'height': 1080}  (fallback)
```

### 3.3 Resizing While Session Is Active

**Important**: The dashboard frontend has a debounced resize handler that POSTs new dimensions to `/api/resolution` whenever the browser window changes size. However, the currently open Chromium session tab does **not** resize dynamically — the resolution is locked in at session start. This is a known limitation, not a bug.

Manual test:

1. Open dashboard, connect to a VM (note the window size)
2. While the VM session is active in a separate Chromium tab, resize the dashboard browser window
3. Open a **second** VM from the dashboard at the new size
4. Verify the second VM opens at the new viewport dimensions, while the first session retains its original size

```bash
# Verify the dashboard's /api/resolution updates on resize
# (Check server logs while resizing)
python dashboard_server.py 2>&1 | grep -i resolution
```

### 3.4 Unicode and Case Variation in Resolution Strings

```bash
# All three should produce identical results (the _RES_RE regex supports all)
python -c "
from azure_wrapper.resolution import parse
print(parse('1920x1080'))   # ASCII x
print(parse('1920X1080'))   # uppercase X
print(parse('1920×1080'))    # Unicode multiplication sign
"
# Expected: all three return {'width': 1920, 'height': 1080}
```

### 3.5 Config Persistence

```bash
# Set resolution via config file
cat > /tmp/test_resolution_config.py << 'EOF'
from azure_wrapper.config import AzureConfig
import os
os.environ['AZURE_RESOLUTION'] = 'qhd'
cfg = AzureConfig.from_env()
print(f"resolution={cfg.resolution}, viewport={cfg.get_viewport()}")
EOF
python /tmp/test_resolution_config.py
# Expected: resolution=qhd, viewport={'width': 2560, 'height': 1440}
```

---

## Part 4 — Window Manager Adaptation (i3 / fluxbox)

This is the key live-VM test: does the WM handle resolution changes correctly?

### 4.1 Test Setup

Ensure the VM has i3 or fluxbox running:
```bash
# Inside the VM via SSH or RDP:
echo $DESKTOP_SESSION    # should show i3 or fluxbox
pgrep -a i3              # verify i3 is running
# or
pgrep -a fluxbox         # verify fluxbox is running
```

### 4.2 Resolution Switching

For each resolution (1024×768, 1280×720, 1920×1080, 3840×2160):

1. Launch a new VM session at the target resolution via the dashboard or `AVDClient`
2. Inside the VM:
   ```bash
   # Check what the guest OS thinks the resolution is
   xrandr | grep '*'     # X11
   # or
   wlr-randr             # Wayland
   ```
3. Open 3 terminal windows and tile them (i3: Mod+Enter × 3)
4. Open a web browser (Firefox) and maximize it
5. Verify:
   - Windows fill the available space (no gaps at edges)
   - No windows render partially off-screen
   - i3 bar / fluxbox toolbar is fully visible, not truncated
   - Text is legible and UI elements are correctly sized

### 4.3 Application Rendering at Each Resolution

| Test | 1024×768 | 1280×720 | 1920×1080 | 3840×2160 |
|------|----------|----------|-----------|-----------|
| Terminal (alacritty/kitty) | Text readable | ✓ | ✓ | Text not too tiny? |
| Firefox ~5 tabs | No horizontal scroll | ✓ | ✓ | Tabs visible? |
| VS Code / editor | ✓ | ✓ | ✓ | UI scaling OK? |
| File manager (thunar/pcmanfm) | ✓ | ✓ | ✓ | Icons visible? |
| System tray / i3status | Fully visible | ✓ | ✓ | Not cut off? |

For 4K specifically: if UI elements are too small, note it — the system may need DPI scaling awareness in a future iteration.

---

## Part 5 — Screen Sharing (OBS NDI) Resolution Check

```bash
# Run setup script and verify the NDI profile uses the resolved resolution
cd /home/mark/code/workee
python -c "
from azure_wrapper.resolution import get_resolution
res = get_resolution()
print(f'Base resolution: {res[\"width\"]}x{res[\"height\"]}')
print(f'Output resolution: {res[\"width\"]}x{res[\"height\"]}')
"

# Then run setup with that resolution
# ./screen_sharing/setup_host_ndi.sh
# → OBS → Settings → Video → verify Base and Output match the resolved dimensions
```

---

## Part 6 — Issue Report Template

If you find any issues during testing, document them here and file a GitHub issue:

```
### Resolution: [e.g. 3840×2160]
### Method: [dashboard auto-detect / AZURE_RESOLUTION env / --resolution flag]
### Expected: [what should happen]
### Actual: [what happened — include screenshots if possible]
### Steps to reproduce:
1.
2.
3.
### Log output: (paste relevant log lines)
```

---

## Completion Checklist

- [ ] Automated tests pass: `pytest test_resolution_integration.py test_azure_wrapper.py` (182 tests)
- [ ] 1024×768 — dashboard auto-detect + session launch OK — no artifacts
- [ ] 1280×720 — dashboard auto-detect + session launch OK — no artifacts
- [ ] 1920×1080 — dashboard auto-detect + session launch OK — no artifacts
- [ ] 3840×2160 — dashboard auto-detect + session launch OK — no artifacts
- [ ] Custom resolution 2560×1080 (ultrawide) via env var works
- [ ] Invalid overrides fall back gracefully (no crash, no hang)
- [ ] Missing display server → FALLBACK (1920×1080) used
- [ ] Mid-session resize: old session retains size, new session uses new size
- [ ] Unicode '×' and uppercase 'X' in WxH strings parse correctly
- [ ] i3 or fluxbox tiles windows correctly at each resolution
- [ ] Terminal, browser, editor, file manager render correctly
- [ ] OBS NDI profile resolution matches resolver output
- [ ] No visual artifacts: scrollbars, black bars, off-screen elements, blur
- [ ] All issues documented with screenshots in the issue tracker
