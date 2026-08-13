# Host-to-VM Screen Sharing for Microsoft Teams

**Date:** 2026-08-10
**Context:** User runs Microsoft Teams inside a VM (QEMU/KVM, Windows guest, Linux host) and needs to share the host machine's desktop into Teams meetings.

---

## TL;DR Recommendation

**Use NDI network streaming for a zero-hardware software solution, or a cheap HDMI-to-USB capture card for a plug-and-play hardware solution.** The HDMI capture card is the simplest and most foolproof; NDI is the best software-only approach.

---

## Method 1: NDI Network Streaming (Software — Recommended)

**Concept:** Capture the host desktop as an NDI video stream, then consume it inside the Windows VM as a virtual webcam that Teams sees as a camera source.

**How it works:**
1. **Host (Linux):** Install OBS Studio + the [DistroAV NDI plugin](https://github.com/DistroAV/DistroAV) (formerly obs-ndi). Configure OBS to capture the host desktop as a scene. Enable NDI Output — the desktop becomes an NDI source on the local network.
   - *Alternative:* Use the standalone [NDI Screen Capture HX](https://ndi.video/tools/screen-capture/) tool if on Windows/macOS host. For Linux hosts, OBS + DistroAV is the path.
2. **Guest (Windows VM):** Install the free [NDI Tools](https://ndi.video/tools/) suite (v6.3.2). Launch **NDI Webcam Input** — it presents any NDI source on the network as a virtual webcam device (DirectShow).
3. **Teams:** Select "NDI Webcam Input" as the camera in Teams. Your host desktop appears as your video feed.

**For audio:** Use PipeWire or JACK on the Linux host to route system audio into OBS. NDI carries audio alongside video. On the guest, the virtual webcam source includes the audio track.

**Network requirements:**
- VM must be on the same IP subnet as the host (use **bridged networking**, not NAT). NAT can work but NDI's mDNS discovery may fail; you'd need to manually point the Webcam Input at the host's IP.
- Bandwidth: NDI is lightly compressed — ~100–200 Mbps for 1080p60. On a local virtual bridge this is effectively free (no physical wire needed).
- Security: NDI is LAN-only by design. No ports exposed to the internet.

| Pros | Cons |
|------|------|
| Zero hardware cost (free tools) | NDI Tools are Windows/macOS only on the receive side (fine for Windows guest) |
| Low latency: ~1–2 frames (16–33 ms at 60 fps) | Bandwidth-heavy on physical networks (irrelevant for VM-to-host) |
| Carries audio + video in one stream | OBS + DistroAV setup on Linux host requires some configuration |
| High quality (up to 4K) | NDI mDNS discovery may need manual IP entry behind NAT |
| Works with any hypervisor (QEMU, VirtualBox, VMware) | |
| Mature, broadcast-industry technology | |

**Implementation effort:** Medium (2–4 hours for first-time setup). Install OBS + plugin on host, NDI Tools on guest, configure network bridging.

---

## Method 2: HDMI-to-USB Capture Card (Hardware — Simplest)

**Concept:** Physically capture the host GPU's HDMI output with a cheap USB capture dongle, pass the USB device through to the VM, and it appears as a native UVC webcam.

**How it works:**
1. **Hardware:** Buy an HDMI-to-USB capture card (~$15–30 AUD on Amazon/eBay). Common chipsets: MS2130, MS2109, MacroSilicon. These present as standard UVC (USB Video Class) devices — no drivers needed on any OS.
2. **Host:** Plug the capture card into a USB port. Connect an HDMI cable from your GPU output to the capture card's HDMI input.
3. **QEMU/Virt-Manager:** Pass the USB device through to the Windows VM (virt-manager → Add Hardware → USB Host Device).
4. **Guest (Windows):** The capture card appears as a native webcam under "Cameras" in Settings.
5. **Teams:** Select the capture card as your camera. The host desktop is your video feed.

**For audio:** HDMI carries audio. Most capture cards expose both a video and audio device over USB. Configure Windows sound settings to use the capture card's audio input as the microphone, or set up audio routing separately.

**For screen-share quality (not webcam):** Teams treats this as a camera feed, not a screen share. Resolution is typically limited to 1080p. For true screen-share fidelity, combine with OBS on the guest: capture the UVC device in OBS, then use OBS Virtual Camera to Teams (allows scaling/cropping).

| Pros | Cons |
|------|------|
| Plug and play — zero software config | Requires hardware purchase (~$15–30) |
| Native UVC device, no special drivers | Limited to webcam resolution/quality (typically 1080p30) |
| Zero latency | Occupies a physical USB port and HDMI output |
| Works with any hypervisor, any guest OS | HDMI cable clutter |
| Survives reboots, hypervisor changes | Need a free HDMI port on GPU (or use HDMI dummy plug + extended desktop) |
| No network configuration needed | |

**Implementation effort:** Very low (15 minutes). Buy dongle, plug in, USB passthrough, done.

---

## Method 3: OBS + Local RTMP Server (Software — Fallback)

**Concept:** Stream the host desktop to a local RTMP server, then play the stream back in the guest and capture it as a virtual webcam via OBS.

**How it works:**
1. **Host (Linux):** Install nginx with the RTMP module, or a simpler server like [MediaMTX](https://github.com/bluenviron/mediamtx) (single binary, no config needed for basic RTMP). Start the RTMP server.
2. **Host:** OBS Studio streams the desktop capture to `rtmp://<host-ip>/live/desktop`.
3. **Guest (Windows):** OBS Studio on the guest adds a "Media Source" pointing to the RTMP URL (or use VLC to play it). Enable OBS Virtual Camera.
4. **Teams:** Select "OBS Virtual Camera" as the camera.

**Alternative:** Skip OBS on the guest — use ffplay/VLC to play the RTMP stream in a window, then use Teams "Share Window" to share that playback window. Messier but avoids the webcam resolution limit.

| Pros | Cons |
|------|------|
| All free and open-source | 2–5 second latency (RTMP buffering) |
| Very flexible — can add overlays, multiple scenes | More complex setup (nginx config or MediaMTX setup) |
| Works over any network topology | "Share window" approach is clunky for screen sharing |
| Industry-standard streaming protocol | OBS on both host AND guest needed for clean virtual-camera path |

**Implementation effort:** High (4–8 hours). Configure RTMP server, OBS on both sides, tune latency.

---

## Method 4: Virtual Display Driver + RDP/VNC Mirroring (Software — Experimental)

**Concept:** Create a fake second monitor on the Windows guest, then mirror the host desktop onto it so Teams can screen-share that "monitor."

**How it works:**
1. **Guest (Windows):** Install [Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver) — creates a software-only monitor in Windows (e.g., a 1920x1080 virtual display).
2. **Host:** Use a tool like `ffmpeg` with x11grab to capture the host desktop and push it to the guest via... some mechanism. This is the hard part. Options:
   - RDP *from guest to host* and display the RDP session on the virtual monitor (full-screen RDP window positioned on the virtual display).
   - Spice/virt-viewer in reverse (not supported).
   - Custom ffmpeg → network → guest receives and renders full-screen on virtual display.
3. **Teams:** Screen-share the virtual monitor.

| Pros | Cons |
|------|------|
| True screen sharing (not webcam) — full resolution, proper frame rate | Reverse RDP is convoluted — you're remoting from the guest back to the host |
| Once working, very clean | High setup complexity, fragile |
| Virtual Display Driver is free and well-maintained | Spice reverse-display not supported |
| | Not recommended unless you specifically need screen-share vs. webcam quality |

**Implementation effort:** Very high (8+ hours). Experimental, fragile, many moving parts. **Not recommended** unless webcam-quality is unacceptable.

---

## Comparison Matrix

| Criterion | NDI Streaming | HDMI Capture Card | RTMP Server | Virtual Display + RDP |
|-----------|--------------|-------------------|-------------|----------------------|
| Cost | Free | ~$15–30 AUD | Free | Free |
| Setup time | 2–4 hours | 15 min | 4–8 hours | 8+ hours |
| Latency | ~1–2 frames | Zero | 2–5 seconds | 1–2 seconds |
| Quality | Up to 4K | 1080p30 typical | Configurable | Full desktop res |
| Audio support | Built-in | Via HDMI | Built-in | Separate routing |
| Reliability | High | Very high | Medium | Low |
| Hypervisor-agnostic | Yes | Yes | Yes | KVM-only (Spice) |
| Appears as | Webcam | Webcam | Webcam or window | Real display (screen share) |

---

## Recommendation

**Primary pick: NDI Network Streaming (Method 1).** It's free, software-only, well-proven in broadcast, and works reliably. The "webcam vs. screen share" distinction is the main trade-off — Teams treats it as a camera feed (lower max resolution, subject to Teams video processing), but for most meeting scenarios this is perfectly adequate. Use bridged networking on the VM for zero-config mDNS discovery.

**If webcam quality is unacceptable: HDMI Capture Card (Method 2).** Buy the hardware and get a zero-latency, zero-software-fuss solution. Combine with an HDMI dummy plug (~$5) if you don't have a spare GPU output — the host extends its desktop onto the dummy monitor, the capture card grabs that display, and the VM sees it as a camera. You can even set this up so the dummy display is a clean "presentation desktop" with only the windows you want to share.

**Audio routing for either method:** On the Linux host, create a PipeWire virtual sink that combines system audio + microphone. Route this into OBS (NDI method) or into the HDMI audio stream (capture card method). The guest receives synchronized audio + video.

---

## References

- [NDI Tools](https://ndi.video/tools/) — Free NDI Screen Capture, Webcam Input, Virtual Input
- [DistroAV (OBS NDI plugin)](https://github.com/DistroAV/DistroAV) — GPLv2, actively maintained
- [OBS Virtual Camera + v4l2loopback (Linux)](https://github.com/umlaeute/v4l2loopback) — Required for OBS Virtual Camera on Linux host
- [Virtual Display Driver (Windows)](https://github.com/VirtualDrivers/Virtual-Display-Driver) — Software-only virtual monitors
- [MediaMTX](https://github.com/bluenviron/mediamtx) — Zero-config RTMP/RTSP/SRT server (single binary)
- [MS2130 HDMI Capture Card](https://www.amazon.com/s?k=hdmi+to+usb+capture+card+ms2130) — Common chipset, ~$15–30, driverless UVC
