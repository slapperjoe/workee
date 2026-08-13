# Screen Sharing User Guide

## Host Desktop → Windows VM → Microsoft Teams

This guide walks you through sharing your Linux host desktop into Microsoft
Teams running inside a Windows virtual machine. The solution uses **NDI network
streaming** — it is fast (1–2 frames of latency), costs nothing, and requires
no extra hardware.

**How it works (30-second version):** OBS Studio captures your Linux desktop
and broadcasts it as an NDI video stream on your local network. Inside the
Windows VM, a free tool called NDI Webcam Input picks up that stream and
presents it as a virtual webcam. Microsoft Teams sees it as a regular camera
and you select it like any other webcam.

```
 ┌─── Host (Linux) ──────────────┐     ┌─── Guest VM (Windows) ────────┐
 │                                │     │                               │
 │  Your Desktop  ──► OBS Studio  │     │  NDI Webcam Input ──► Teams  │
 │                   + DistroAV   │◄───►│    (virtual webcam)           │
 │                                │ NDI │                               │
 │  ndi_control.py  (optional)    │     │                               │
 └────────────────────────────────┘     └───────────────────────────────┘
```

---

## Prerequisites

Before you begin, make sure you have:

| Component | Minimum Requirement |
|-----------|-------------------|
| **Host OS** | Arch Linux (or derivative with `pacman`) |
| **VM Software** | QEMU/KVM with virt-manager (or any hypervisor that can pass bridged networking) |
| **Guest OS** | Windows 10 or 11 (64-bit) |
| **Microsoft Teams** | Any recent version (desktop app or web) |
| **Host RAM** | 8 GB minimum (OBS + VM running together) |
| **VM Networking** | Bridged networking strongly recommended (NAT works with manual IP) |
| **Host free disk** | ~3 GB for OBS, build tools, and NDI SDK |
| **Guest free disk** | ~300 MB for NDI Tools |

---

## Quick-Start Checklist

Use this checklist each time you want to share your screen. The one-time setup
in the next section only needs to be done once.

### One-time setup (first use only)

- [ ] Run the host setup script: `./screen_sharing/setup_host_ndi.sh`
- [ ] On the Windows VM, run: `.\screen_sharing\setup_guest_ndi.ps1`
- [ ] In OBS: enable the WebSocket server (Tools → obs-websocket Settings → Enable)
- [ ] Verify the VM uses bridged networking (virt-manager → VM → NIC → Bridge device)

### Every session (daily use)

- [ ] Start OBS on the host: `obs --profile NDI_ScreenShare --collection NDI_ScreenShare`
- [ ] Confirm NDI Webcam Input is running on the guest (system tray icon)
- [ ] Join a Teams meeting → Camera dropdown → select **NDI Webcam Input**
- [ ] Your host desktop appears as your video feed

---

## Step-by-Step Setup

### Part A — Host Setup (Arch Linux)

This is a one-time operation. Open a terminal on your Linux host and run:

```sh
cd /path/to/this/project
chmod +x screen_sharing/setup_host_ndi.sh
./screen_sharing/setup_host_ndi.sh
```

The script installs and configures five things automatically:

1. **System packages** — OBS Studio 32+, v4l2loopback virtual camera, PipeWire
   JACK, and build tools (GCC, CMake, Git).
2. **Virtual camera device** — creates `/dev/video10` so OBS can present its
   output as a webcam on the host if desired (optional — NDI does not need it).
3. **NDI SDK v6** — downloads the free SDK from ndi.tv and installs headers and
   libraries to `/usr/local/ndi`.
4. **DistroAV OBS plugin** — builds the NDI output plugin from source and
   installs it to `/usr/lib/obs-plugins/`.
5. **OBS profile** — creates a ready-to-use profile called `NDI_ScreenShare`
   with 1080p60 desktop capture and NDI output pre-configured.

**Useful flags:**

```sh
./setup_host_ndi.sh --dry-run        # See what will happen without doing it
./setup_host_ndi.sh --no-obs          # Skip OBS install (if already installed)
./setup_host_ndi.sh --no-v4l2         # Skip virtual camera setup
```

**Expected duration:** 5–15 minutes (mostly downloading and compiling DistroAV).

**Verify the setup:**

```sh
# The virtual camera device exists
ls /dev/video10

# The NDI SDK library is findable
ldconfig -p | grep ndi

# The OBS plugin file exists
ls /usr/lib/obs-plugins/ndi.so
```

### Part B — Guest Setup (Windows VM)

Open PowerShell as Administrator inside your Windows VM and run:

```powershell
cd \path\to\this\project
.\screen_sharing\setup_guest_ndi.ps1
```

The script does the following:

1. **Downloads and installs** NDI Tools v6.3.2 silently (a ~250 MB download).
2. **Enables remote NDI reception** in NDI Access Manager so the VM can see
   NDI streams from the host.
3. **Configures NDI Webcam Input** to automatically connect to the host's
   stream named `HostDesktop`.
4. **Launches NDI Webcam Input** — a DirectShow virtual webcam appears in
   Windows.
5. **Verifies** the virtual camera device is visible to Windows.

**Expected duration:** 2–4 minutes.

**If you see "NDI source not found":** your VM is probably using NAT networking
instead of bridged. Either switch to bridged networking (recommended,
see Part D below) or pass the host's IP manually:

```powershell
# Find the host bridge IP first (run on host):
ip addr show virbr0 | grep inet

# Then on the guest:
.\screen_sharing\setup_guest_ndi.ps1 -HostIP 192.168.122.1
```

### Part C — Enable OBS WebSocket (for ndi_control.py)

The Python control script lets you start, stop, and check the NDI output
status without touching the OBS GUI. To use it, enable the WebSocket server
in OBS once:

1. Launch OBS: `obs --profile NDI_ScreenShare --collection NDI_ScreenShare`
2. Go to **Tools → obs-websocket Settings**
3. Check **Enable WebSocket server**
4. Leave the port as `4455` and set a password if you want (optional)

Test the connection:

```sh
python screen_sharing/ndi_control.py status
```

You should see OBS version, streaming status, and the current scene.

### Part D — Network Configuration

NDI uses mDNS (multicast DNS) to discover streams on the local network. The VM
must be on the **same subnet** as the host for this to work.

**Recommended: Bridged networking (libvirt / virt-manager)**

1. Open virt-manager
2. Select your Windows VM → **Open** → **Show virtual hardware details** (the
   lightbulb icon)
3. Select the **NIC** device
4. Set **Network source** to `Bridge device` and pick `virbr0`
5. Click **Apply**

The VM now gets an IP on the same subnet as the host. NDI discovery works
automatically.

**Fallback: NAT networking (default)**

If you keep NAT, NDI auto-discovery will fail but you can still connect by
specifying the host's IP. Run the guest setup with the `-HostIP` flag:

```powershell
.\screen_sharing\setup_guest_ndi.ps1 -HostIP <host-bridge-ip>
```

The host's bridge IP is usually `192.168.122.1`. Find it with:

```sh
ip addr show virbr0 | grep 'inet '
```

---

## Daily Use

Once everything is set up, here is the routine:

### 1. Start OBS on the host

```sh
obs --profile NDI_ScreenShare --collection NDI_ScreenShare
```

OBS starts minimized (or in the background if you prefer). The NDI output
named `HostDesktop` begins broadcasting immediately.

**Alternative (no GUI):** Use the Python script to start the stream:

```sh
python screen_sharing/ndi_control.py start
```

### 2. Verify NDI Webcam Input is running (guest)

Look for the NDI icon in the Windows system tray. If it is not running:

```powershell
# Launch it manually
& "C:\Program Files\NDI\NDI Webcam Input\NDIWebcamInput.exe"
```

### 3. Join a Teams meeting

1. Open Microsoft Teams on the Windows VM.
2. Join or start a meeting.
3. Click the **camera dropdown** in the meeting toolbar.
4. Select **NDI Webcam Input**.
5. Your host desktop appears as your video feed.

**Tip:** When you want to stop sharing, either close OBS on the host or switch
Teams to a different camera (or turn off your video). NDI Webcam Input keeps
running — it just sends a blank frame when there is no source.

---

## Controlling OBS from the Terminal

The `ndi_control.py` script talks to OBS via WebSocket. It is useful for
automation, scripting, or when you prefer a terminal over the OBS GUI.

```sh
# Check current state
python screen_sharing/ndi_control.py status

# Start and stop NDI output
python screen_sharing/ndi_control.py start
python screen_sharing/ndi_control.py stop

# Switch which display to capture (0 = primary, 1 = secondary, etc.)
python screen_sharing/ndi_control.py display 1

# Capture a specific window instead of the whole desktop
python screen_sharing/ndi_control.py window "Firefox"

# Switch scenes (if you have multiple scenes set up in OBS)
python screen_sharing/ndi_control.py scene "Presentations"
```

**Connecting to OBS on a different machine:**

If you run OBS on another host, set environment variables:

```sh
export OBS_WS_HOST=192.168.1.50
export OBS_WS_PORT=4455
export OBS_WS_PASSWORD=yourpassword
python screen_sharing/ndi_control.py status
```

Or pass them as flags:

```sh
python screen_sharing/ndi_control.py --host 192.168.1.50 --password secret status
```

---

## Audio Setup (Optional)

By default, NDI carries video only. To share audio (system sounds or
microphone) alongside the desktop:

### Host-side audio routing

Create a PipeWire virtual sink that mixes system audio and microphone, then
add it as an audio source in OBS:

```sh
# Create a virtual audio sink
pactl load-module module-null-sink sink_name=ndi_mix

# Route application audio into the mix
pw-link <your-app-output> ndi_mix:playback_1

# Route your microphone into the mix
pw-link <your-mic-source> ndi_mix:playback_2
```

Then in OBS: **Audio Mixer** (dock at bottom) → click the gear icon →
**Add** → select `ndi_mix.monitor` as an Audio Output Capture source.

The NDI stream now carries audio. On the Windows guest, NDI Webcam Input
exposes both a video device and an audio device. You may need to select the
NDI audio input as your microphone in **Windows Sound Settings → Input**.

---

## Troubleshooting

### "NDI Webcam Input" does not appear in Teams

1. **Verify the virtual camera exists.** Run this in PowerShell on the guest:
   ```powershell
   Get-PnpDevice -Class Camera | Where-Object Name -like "*NDI*"
   ```
   You should see a device like "NDI Webcam Input" or "NewTek NDI Video."
   If not, restart NDI Webcam Input from the Start Menu or system tray.

2. **Restart Teams.** Teams only scans for cameras at startup. Close Teams
   completely (right-click the system tray icon → Quit) and reopen it.

3. **Check the Teams web app.** Some Teams desktop versions have stricter
   camera enumeration. Try `teams.microsoft.com` in Chrome or Edge.

### No NDI source found on the guest

NDI Webcam Input shows "No Source" or a black screen.

1. **Check networking.** The VM must be on the same subnet as the host.
   ```sh
   # On host — verify the VM is bridged:
   virsh domiflist <vm-name>
   ```
   If the type is `network` and source is `default`, you are on NAT.
   Switch to bridged (see Part D above) or use manual IP.

2. **Verify the host is broadcasting.**
   In OBS, go to **Tools → NDI Output Settings**. The Main Output should say
   "Active" and the stream name should be `HostDesktop`.

3. **Windows Firewall.**
   NDI uses UDP ports 5960 and up. If you have a third-party firewall,
   make sure it allows incoming UDP on these ports from the host subnet.

4. **Try manual IP connection:**
   ```powershell
   .\screen_sharing\setup_guest_ndi.ps1 -HostIP <host-ip>
   ```

### OBS NDI plugin does not load

**Symptom:** OBS launches but **Tools → NDI Output Settings** is missing or
grayed out.

1. Check the plugin file exists:
   ```sh
   ls /usr/lib/obs-plugins/ndi.so
   ```

2. Check the OBS log for errors:
   ```sh
   grep -i ndi ~/.config/obs-studio/logs/*.txt | tail -20
   ```
   Common errors: missing `libndi.so` (NDI SDK not in `ldconfig`),
   wrong architecture (32-bit vs 64-bit), or build mismatch.

3. Re-run `sudo ldconfig` after installing the NDI SDK.

### Virtual camera module (v4l2loopback) does not load

```sh
sudo modprobe v4l2loopback
# If you see "Required key not available":
```

This happens when Secure Boot is enabled in the BIOS. Either:
- Sign the kernel module with your Secure Boot key, or
- Disable Secure Boot in the BIOS (simpler).

**Note:** v4l2loopback is only needed if you want OBS to also provide a virtual
camera on the host itself. The NDI path to the guest does not use it.

### Video quality is low or pixelated in Teams

Teams applies its own compression to webcam feeds regardless of the source
quality. A few things help:

1. **Set Teams camera quality to High:**
   Teams → Settings → Devices → Camera → toggle video quality to "High."
   This is a per-device setting.

2. **Check OBS video settings:**
   OBS → Settings → Video. Confirm Output Resolution is 1920×1080 and FPS is
   60. The NDI profile already sets this, but verify it took effect.

3. **Teams bandwidth limit.**
   Teams caps webcam streams at ~1.5 Mbps regardless of source. If you need
   true lossless screen sharing for detailed work (code, CAD, design), the
   NDI-as-webcam approach has this inherent ceiling. Consider an HDMI-to-USB
   capture card as an alternative (see [research doc](../host-to-vm-screen-sharing-research.md)).

### Performance: OBS or VM feels sluggish

- **Reduce OBS output resolution** to 720p: OBS → Settings → Video → set
  both Base and Output to 1280×720. This cuts NDI bandwidth by more than half.
- **Lower the frame rate** from 60 to 30 FPS.
- **Close GPU-heavy applications** on the host while sharing (games, video
  editors, 3D renders). OBS uses GPU encoding which competes with other apps.
- **Assign more CPU cores to the VM** in virt-manager if the guest feels slow.

### Audio not coming through on the guest

1. Confirm OBS has an audio source configured. Open OBS and check the
   **Audio Mixer** panel — you should see at least one active audio source.
2. In NDI Webcam Input on the guest, right-click the system tray icon and
   check that **Audio** is enabled for the selected source.
3. In Windows Sound Settings, set the NDI audio device as the default
   microphone or communication device.

---

## Alternative: HDMI Capture Card

If you want a simpler, zero-software solution with zero latency:

1. Buy an HDMI-to-USB capture card (~$15–30 AUD, MS2130 chipset).
2. Plug the capture card into the host's HDMI output and a USB port.
3. In virt-manager, pass the USB device through to the VM
   (**Add Hardware → USB Host Device → select the capture card**).
4. The VM sees it as a native webcam. Select it in Teams.

No software install, no network config, zero latency. The trade-off is a
physical device and typically lower resolution (1080p30).

See `host-to-vm-screen-sharing-research.md` for a full comparison of four
different methods.

---

## Files Reference

| File | What It Does |
|------|-------------|
| `screen_sharing/setup_host_ndi.sh` | One-shot host setup (Arch Linux): installs OBS, NDI SDK, DistroAV, creates profile |
| `screen_sharing/setup_guest_ndi.ps1` | One-shot guest setup (Windows): installs NDI Tools, configures Webcam Input |
| `screen_sharing/ndi_control.py` | Python CLI for OBS control (start/stop/status/scene/display/window) |
| `screen_sharing/README.md` | Technical reference and architecture overview |
| `screen_sharing/USER_GUIDE.md` | This document |
| `host-to-vm-screen-sharing-research.md` | Full research: four methods compared with trade-offs |
