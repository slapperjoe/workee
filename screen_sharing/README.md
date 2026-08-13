# Screen Sharing: Host Desktop → VM → Microsoft Teams

NDI-based screen sharing that captures the Linux host desktop and presents
it as a virtual webcam inside a Windows VM running Microsoft Teams. Zero
hardware cost, low latency (~1-2 frames), 1080p60 quality.

## Architecture

```
┌─────────────────────────────────┐    ┌──────────────────────────────┐
│         HOST (Linux)            │    │      GUEST VM (Windows)      │
│                                 │    │                              │
│  Desktop ──► OBS Studio ──► NDI │    │  NDI ──► NDI Webcam Input   │
│              + DistroAV         │◄───│                              │
│              + PipeWire audio   │LAN │         DirectShow virtual   │
│                                 │    │         camera device        │
│  ndi_control.py (websocket)     │    │              │               │
│                                 │    │              ▼               │
│                                 │    │     Microsoft Teams          │
│                                 │    │     Select "NDI Webcam       │
│                                 │    │     Input" as camera         │
└─────────────────────────────────┘    └──────────────────────────────┘
```

**Key properties:**
- **Network:** NDI runs over the VM's bridged network (no physical wire).
  Bandwidth on a local virtio bridge is effectively free (~100 Mbps unused).
- **Latency:** 1-2 frames (16-33 ms at 60 fps) — imperceptible in meetings.
- **Audio:** PipeWire routes system audio into OBS; NDI carries A+V together.
- **Quality:** Up to 4K at 60 fps (configured in OBS profile).
- **Appearance in Teams:** As a camera feed (not screen share), subject to
  Teams' video quality limits. For most meetings, 1080p30 webcam quality is
  perfectly adequate.

## Quick start (5 minutes after setup)

### 1. Host (Linux) — start desktop capture

```sh
# One-time setup (5-30 min depending on internet speed):
./screen_sharing/setup_host_ndi.sh

# Every session: launch OBS with the NDI profile
obs --profile NDI_ScreenShare --collection NDI_ScreenShare

# Or control via the Python script:
python screen_sharing/ndi_control.py start
python screen_sharing/ndi_control.py status
```

### 2. Guest (Windows VM) — receive the NDI stream

```powershell
# One-time setup (~2 min download + install):
.\screen_sharing\setup_guest_ndi.ps1

# If mDNS discovery fails (NAT networking), specify the host IP:
.\screen_sharing\setup_guest_ndi.ps1 -HostIP 192.168.122.1
```

### 3. Microsoft Teams — select the camera

1. Join or start a Teams meeting.
2. Click the camera dropdown → select **"NDI Webcam Input"**.
3. Your host desktop appears as your video feed.

---

## Detailed setup instructions

### Host setup (Linux)

**Prerequisites:**
- Arch Linux (or derivative with pacman)
- `sudo` access
- Internet for package/NDI SDK download

**What `setup_host_ndi.sh` does:**

| Step | What it installs/configures |
|------|----------------------------|
| 1 | System packages: OBS Studio 32+, v4l2loopback, PipeWire JACK, build tools |
| 2 | v4l2loopback kernel module — virtual camera at `/dev/video10` |
| 3 | NDI SDK v6 from ndi.tv — headers + libndi.so to `/usr/local/ndi` |
| 4 | DistroAV OBS plugin — builds from source, installs to `/usr/lib/obs-plugins/` |
| 5 | OBS profile + scene collection — 1080p60 desktop capture + NDI output |

**Options:**

```sh
./setup_host_ndi.sh --dry-run      # preview without installing
./setup_host_ndi.sh --no-obs       # skip OBS (already installed)
./setup_host_ndi.sh --no-v4l2      # skip virtual camera (not needed for NDI)
```

**Manual verification after setup:**

```sh
# Check v4l2loopback
ls /dev/video10

# Check NDI SDK
ldconfig -p | grep ndi

# Check DistroAV plugin
ls /usr/lib/obs-plugins/ndi.so

# Launch OBS
obs --profile NDI_ScreenShare --collection NDI_ScreenShare
```

### Guest setup (Windows VM)

**Prerequisites:**
- Windows 10 or 11 (x64)
- PowerShell 5.1+ (built in)
- Internet for NDI Tools download (~250 MB)

**What `setup_guest_ndi.ps1` does:**

| Step | Action |
|------|--------|
| 1 | Downloads and silently installs NDI Tools v6.3.2 |
| 2 | Enables remote NDI source reception in NDI Access Manager |
| 3 | Configures NDI Webcam Input to auto-connect to "HostDesktop" source |
| 4 | Launches NDI Webcam Input (virtual webcam device) |
| 5 | Verifies the DirectShow virtual camera is visible to Windows |

**If running behind NAT networking:**

The VM's network must be bridged for NDI's mDNS auto-discovery to work.
If you're using NAT, pass the host's bridge IP explicitly:

```powershell
.\setup_guest_ndi.ps1 -HostIP 192.168.122.1
```

Find the host's bridge IP with `ip addr show virbr0` on the Linux host.

### Audio routing (optional but recommended)

**On the host**, create a PipeWire virtual sink that combines system audio
and microphone, then route it into OBS:

```sh
# Create a null sink for merging
pactl load-module module-null-sink sink_name=ndi_mix

# Route system audio to the mix
pw-link <app-output> ndi_mix:playback_1

# Route microphone to the mix
pw-link <mic-source> ndi_mix:playback_2

# In OBS: Audio Mixer → Settings → add ndi_mix.monitor as an audio source
```

The NDI stream carries the audio track alongside video. On the Windows guest,
NDI Webcam Input exposes both a video and audio device — set the audio device
as your microphone in Windows Sound settings if needed.

### Network configuration

NDI requires the VM to be on the same subnet as the host. Two options:

**(A) Bridged networking (recommended):**
In virt-manager → VM → NIC → Network source: "Bridge device" (e.g. `virbr0`).
The VM gets an IP on the same subnet as the host. mDNS auto-discovery works.
NDI traffic stays on the virtual bridge — no physical network load.

**(B) NAT networking with manual IP:**
Keep the default NAT setup. Run `setup_guest_ndi.ps1 -HostIP <host-ip>`
to bypass mDNS discovery. The host IP is usually `192.168.122.1` on libvirt.

---

## Controlling OBS programmatically

`ndi_control.py` connects to OBS Studio's built-in WebSocket server
(port 4455, no password by default). Enable it in OBS first:
**Tools → obs-websocket Settings → Enable WebSocket server**.

```sh
# Start/stop NDI output
python screen_sharing/ndi_control.py start
python screen_sharing/ndi_control.py stop

# Check status
python screen_sharing/ndi_control.py status

# Switch scenes (if you create multiple capture scenes in OBS)
python screen_sharing/ndi_control.py scene "Presentations"

# Switch which display is captured
python screen_sharing/ndi_control.py display 1

# Capture a specific window by title
python screen_sharing/ndi_control.py window "Firefox"
```

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `OBS_WS_HOST` | `localhost` | WebSocket host |
| `OBS_WS_PORT` | `4455` | WebSocket port |
| `OBS_WS_PASSWORD` | (empty) | WebSocket password |

---

## Troubleshooting

### "NDI Webcam Input" doesn't appear in Teams camera list

1. Verify the virtual camera device exists:
   ```powershell
   # PowerShell on guest:
   Get-PnpDevice -Class Camera | Where-Object Name -like "*NDI*"
   ```
2. Restart NDI Webcam Input (find it in the system tray).
3. Restart Teams — it only enumerates cameras at startup.

### No NDI source found on guest

1. Check the VM uses bridged networking (not NAT):
   ```sh
   # On host:
   virsh domiflist <vm-name>
   ```
2. If NAT, re-run with explicit IP: `.\setup_guest_ndi.ps1 -HostIP 192.168.122.1`
3. Verify the host is broadcasting NDI:
   ```sh
   # On host — OBS should show "NDI Output: Active" in Tools → NDI Output Settings
   ```
4. Check Windows Firewall isn't blocking NDI (port 5960+ UDP).

### OBS NDI plugin not loaded

```sh
# Check if the plugin .so exists
ls /usr/lib/obs-plugins/ndi.so

# Check OBS logs for plugin load errors:
# ~/.config/obs-studio/logs/
grep -i ndi ~/.config/obs-studio/logs/*.txt | tail -20
```

### v4l2loopback module not loading

```sh
# Secure Boot may block unsigned kernel modules
sudo modprobe v4l2loopback
# If "Required key not available": sign the module or disable Secure Boot in BIOS.
```

### Webcam quality is low in Teams

Teams applies its own video processing (compression, scaling). To improve:
1. In Teams Settings → Devices → Camera, set your camera quality to "High".
2. Consider Method 2 from the research doc (HDMI-to-USB capture card) if you
   need true screen-share fidelity rather than webcam quality.

---

## Alternative: HDMI capture card (hardware)

If NDI doesn't meet your needs, the hardware approach is simpler:

1. Buy a cheap HDMI-to-USB capture card (~$15-30, MS2130 chipset).
2. Plug it into the host's HDMI output and a USB port.
3. Pass the USB device through to the VM (virt-manager → Add Hardware → USB Host Device).
4. The VM sees it as a native UVC webcam. Select it in Teams.

No software config, zero latency. See the [research document](../host-to-vm-screen-sharing-research.md)
for a full comparison of all four methods.

---

## Files

| File | Purpose |
|------|---------|
| `setup_host_ndi.sh` | Host (Arch Linux) one-shot setup script |
| `setup_guest_ndi.ps1` | Guest (Windows) one-shot setup script |
| `ndi_control.py` | Python CLI for OBS WebSocket control (start/stop/status/scene) |

---

## References

- [NDI Tools](https://ndi.video/tools/) — free NDI Webcam Input, Access Manager, Screen Capture
- [DistroAV (OBS NDI plugin)](https://github.com/DistroAV/DistroAV) — GPLv2, actively maintained
- [OBS Studio](https://obsproject.com/) — free, open-source streaming/recording
- [obs-websocket](https://github.com/obsproject/obs-websocket) — built into OBS 28+, port 4455
- [v4l2loopback](https://github.com/umlaeute/v4l2loopback) — Linux virtual camera kernel module
- [PipeWire](https://pipewire.org/) — Linux audio/video routing
