#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup_host_ndi.sh — Host-side NDI screen sharing setup for Arch Linux
#
# Installs and configures OBS Studio + DistroAV NDI plugin so the host
# desktop is available as an NDI video source on the local network. The
# Windows VM guest can then pick it up via NDI Webcam Input and present it
# as a virtual camera to Microsoft Teams.
#
# Usage:
#   chmod +x setup_host_ndi.sh
#   ./setup_host_ndi.sh              # full interactive setup
#   ./setup_host_ndi.sh --no-obs     # skip OBS install (assume already installed)
#   ./setup_host_ndi.sh --dry-run    # print what would be done, don't do it
#
# Requirements:
#   - Arch Linux (or derivative) with pacman
#   - sudo access
#   - Internet connection for package downloads
# ---------------------------------------------------------------------------

set -e  # exit on error

# -- colour helpers -----------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERR]${NC}   $*"; }
step()  { echo -e "\n${BOLD}==>${NC} ${BOLD}$*${NC}"; }

# -- CLI flags ----------------------------------------------------------------
DRY_RUN=false
SKIP_OBS=false
SKIP_V4L2=false
RESOLUTION=""  # e.g. "1920x1080", "desktop", or empty for auto-detect
NDI_SDK_URL="https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz"
DISTROAV_REPO="https://github.com/DistroAV/DistroAV.git"
DISTROAV_TAG="6.0.2"          # latest stable as of 2026

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)     DRY_RUN=true; shift ;;
        --no-obs)      SKIP_OBS=true; shift ;;
        --no-v4l2)     SKIP_V4L2=true; shift ;;
        --resolution)  RESOLUTION="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--dry-run] [--no-obs] [--no-v4l2] [--resolution WxH|preset]"
            echo ""
            echo "  --resolution   Set OBS canvas/output resolution (e.g. 1920x1080, desktop, 4k)"
            echo "                 Default: auto-detect from system, falling back to 1920x1080"
            exit 0 ;;
        *) err "Unknown flag: $1"; exit 2 ;;
    esac
done

# -- helpers ------------------------------------------------------------------
run() {
    if $DRY_RUN; then
        echo "  [DRY-RUN] $*"
    else
        eval "$@"
    fi
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        err "This section requires root. Re-run with sudo or as root."
        exit 1
    fi
}

# -- step 1: system packages --------------------------------------------------
step "1/5  Installing system packages"

PKGS=(
    obs-studio                 # OBS Studio (includes obs-websocket since v28)
    v4l2loopback-dkms          # virtual camera kernel module
    v4l2loopback-utils         # v4l2-ctl for managing virtual devices
    pipewire-jack              # JACK compatibility for PipeWire audio routing
    git base-devel cmake gcc   # build toolchain for DistroAV
)

if $SKIP_OBS; then
    warn "Skipping OBS Studio (--no-obs)"
    PKGS=( "${PKGS[@]:1}" )   # remove first element (obs-studio)
fi

if $SKIP_V4L2; then
    warn "Skipping v4l2loopback (--no-v4l2)"
    PKGS=( "${PKGS[@]:1:3}" "${PKGS[@]:4}" )
fi

info "Packages to install: ${PKGS[*]}"
run "sudo pacman -S --needed --noconfirm ${PKGS[*]}"

# -- step 2: load v4l2loopback kernel module ----------------------------------
if ! $SKIP_V4L2; then
    step "2/5  Configuring v4l2loopback virtual camera"

    # Create a virtual video device at /dev/video10 with exclusive_caps=1
    # (required for Chrome/Teams to recognise it as a camera).
    MODPROBE_CONF="/etc/modprobe.d/v4l2loopback.conf"
    V4L2_OPTS="options v4l2loopback devices=1 video_nr=10 card_label=\"OBS Virtual Camera\" exclusive_caps=1"

    if ! grep -qF "$V4L2_OPTS" "$MODPROBE_CONF" 2>/dev/null; then
        info "Writing $MODPROBE_CONF"
        run "echo '$V4L2_OPTS' | sudo tee $MODPROBE_CONF"
    else
        info "$MODPROBE_CONF already configured"
    fi

    if ! lsmod | grep -q v4l2loopback; then
        info "Loading v4l2loopback module"
        run "sudo modprobe v4l2loopback"
    else
        info "v4l2loopback already loaded"
    fi

    # Verify device was created
    if [[ -e /dev/video10 ]]; then
        info "Virtual camera device /dev/video10 created"
    else
        warn "/dev/video10 not found — OBS virtual camera will use a different device"
    fi
fi

# -- step 3: install NDI SDK --------------------------------------------------
step "3/5  Installing NDI SDK"

NDI_INSTALL_DIR="/usr/local/ndi"
NDI_TARBALL="/tmp/ndi-sdk-linux.tar.gz"

if [[ -d "$NDI_INSTALL_DIR/lib" ]] && [[ -f "$NDI_INSTALL_DIR/include/Processing.NDI.Lib.h" ]]; then
    info "NDI SDK already installed at $NDI_INSTALL_DIR"
else
    if [[ ! -f "$NDI_TARBALL" ]]; then
        info "Downloading NDI SDK from ndi.tv..."
        info "  URL: $NDI_SDK_URL"
        info "  This is ~25 MB. If the download hangs, open the URL in a browser"
        info "  and place the tarball at $NDI_TARBALL manually."
        run "curl -fSL --retry 3 --retry-delay 5 -o '$NDI_TARBALL' '$NDI_SDK_URL'"
    fi

    info "Extracting NDI SDK to /tmp/ndi-sdk"
    run "mkdir -p /tmp/ndi-sdk"
    run "tar xzf '$NDI_TARBALL' -C /tmp/ndi-sdk"

    # The archive has a versioned directory inside; find it.
    INNER_DIR=$(find /tmp/ndi-sdk -maxdepth 1 -type d -name 'NDI*' -o -name 'ndi*' 2>/dev/null | head -1)
    if [[ -z "$INNER_DIR" ]]; then
        INNER_DIR=$(find /tmp/ndi-sdk -maxdepth 1 -type d ! -name . | head -2 | tail -1)
    fi
    info "Found inner directory: $INNER_DIR"

    info "Copying NDI SDK to $NDI_INSTALL_DIR"
    run "sudo mkdir -p '$NDI_INSTALL_DIR'"
    run "sudo cp -r '$INNER_DIR'/include '$NDI_INSTALL_DIR/'"
    run "sudo cp -r '$INNER_DIR'/lib '$NDI_INSTALL_DIR/'"

    # ldconfig so the runtime linker finds libndi.so
    run "echo '$NDI_INSTALL_DIR/lib' | sudo tee /etc/ld.so.conf.d/ndi.conf"
    run "sudo ldconfig"

    info "NDI SDK installed"
fi

# -- step 4: build & install DistroAV (obs-ndi plugin) ------------------------
step "4/5  Building DistroAV (OBS NDI plugin)"

DISTROAV_DIR="/tmp/DistroAV"

if [[ -f "/usr/lib/obs-plugins/ndi.so" ]] || [[ -f "/usr/lib64/obs-plugins/ndi.so" ]]; then
    info "DistroAV plugin already installed"
else
    if [[ -d "$DISTROAV_DIR/.git" ]]; then
        info "Updating existing DistroAV clone"
        run "cd '$DISTROAV_DIR' && git fetch --tags"
    else
        info "Cloning DistroAV from $DISTROAV_REPO"
        run "git clone --depth 1 --branch '$DISTROAV_TAG' '$DISTROAV_REPO' '$DISTROAV_DIR'"
    fi

    # The CMakeLists needs to find the NDI SDK headers and libs.
    # DistroAV looks for NDI_SDK_DIR or falls back to /usr/local.
    info "Building DistroAV (this may take a few minutes)..."
    run "cd '$DISTROAV_DIR' && mkdir -p build && cd build"
    run "cmake .. -DCMAKE_INSTALL_PREFIX=/usr -DNDI_SDK_DIR='$NDI_INSTALL_DIR'"
    run "make -j\$(nproc)"
    run "sudo make install"

    info "DistroAV plugin installed to /usr/lib/obs-plugins/"
fi

# -- step 5: configure OBS profile and scene collection -----------------------
step "5/5  Configuring OBS for host desktop → NDI output"

# --- Resolve resolution for OBS canvas/output ---
# Priority: --resolution flag > AZURE_RESOLUTION env var > system auto-detect > fallback 1920x1080
_res_spec="${RESOLUTION:-${AZURE_RESOLUTION:-}}"
if command -v python3 &>/dev/null && [[ -f "$SCRIPT_DIR/../azure_wrapper/resolution.py" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    _res_python=$(
        cd "$SCRIPT_DIR/.." && python3 -c "
import sys; sys.path.insert(0, '.')
from azure_wrapper.resolution import get_resolution
res = get_resolution(configured='$_res_spec' if '$_res_spec' else None)
print(f\"{res['width']}x{res['height']}\")
" 2>/dev/null
    )
    if [[ -n "$_res_python" ]] && [[ "$_res_python" =~ ^[0-9]+x[0-9]+$ ]]; then
        OBS_W="${_res_python%x*}"
        OBS_H="${_res_python#*x}"
        info "Resolved OBS resolution: ${OBS_W}x${OBS_H}"
    else
        OBS_W=1920; OBS_H=1080
        warn "Could not resolve resolution; using default 1920x1080"
    fi
else
    # Parse simple resolution string without Python
    OBS_W=1920; OBS_H=1080
    if [[ -n "$_res_spec" ]]; then
        if [[ "$_res_spec" =~ ^([0-9]+)x([0-9]+)$ ]]; then
            OBS_W="${BASH_REMATCH[1]}"; OBS_H="${BASH_REMATCH[2]}"
        fi
    fi
    info "Using OBS resolution: ${OBS_W}x${OBS_H}"
fi

OBS_CONFIG_DIR="${HOME}/.config/obs-studio"
mkdir -p "$OBS_CONFIG_DIR/basic/profiles/NDI_ScreenShare"
mkdir -p "$OBS_CONFIG_DIR/basic/scenes"

# --- OBS Scene Collection JSON (minimal: one Desktop Capture source) ---------
# OBS scene JSON.  One scene "Host Desktop" with a pipewire desktop capture
# source (Screen Capture (PipeWire) on Wayland, or xcomposite on X11).

# Detect session type for the correct capture source
if [[ "$XDG_SESSION_TYPE" == "wayland" ]]; then
    CAPTURE_ID="pipewire-desktop-capture-source"
    CAPTURE_NAME="Screen Capture (PipeWire)"
else
    CAPTURE_ID="xcomposite_input"
    CAPTURE_NAME="Screen Capture (XSHM)"
fi

SCENE_JSON=$(cat <<ENDSCENE
{
  "current_scene": "Host Desktop",
  "current_program_scene": "Host Desktop",
  "scene_order": [
    {"name": "Host Desktop"}
  ],
  "sources": [
    {
      "name": "Desktop Capture",
      "type": "$CAPTURE_ID",
      "settings": {},
      "flags": 0,
      "volume": 1.0,
      "sync": 0,
      "mixers": 1
    }
  ]
}
ENDSCENE
)

info "Writing OBS scene collection"
echo "$SCENE_JSON" > "$OBS_CONFIG_DIR/basic/scenes/NDI_ScreenShare.json"

# --- OBS Profile JSON --------------------------------------------------------
PROFILE_JSON=$(cat <<ENDPROFILE
{
  "Name": "NDI_ScreenShare",
  "Audio": {
    "SampleRate": 48000,
    "ChannelSetup": "Stereo"
  },
  "Video": {
    "Base": [${OBS_W}, ${OBS_H}],
    "Output": [${OBS_W}, ${OBS_H}],
    "FPSCommon": 60,
    "ScaleType": "bicubic"
  },
  "Output": {
    "Mode": "Advanced",
    "Streaming": {
      "Service": "",
      "Server": "",
      "Key": ""
    }
  },
  "NDI": {
    "MainOutput": {
      "Name": "HostDesktop",
      "Groups": "public",
      "Enabled": true,
      "Bandwidth": "highest",
      "AudioEnabled": true
    }
  }
}
ENDPROFILE
)

info "Writing OBS profile"

# OBS stores profiles under basic/profiles/<name>/basic.ini
# The scene collection reference lives in basic.ini of each profile.
# Write an INI-format profile config (the JSON above was informational).
cat > "$OBS_CONFIG_DIR/basic/profiles/NDI_ScreenShare/basic.ini" <<ENDINI
[General]
Name=NDI_ScreenShare
PreviewProgramMode=false

[Video]
BaseCX=${OBS_W}
BaseCY=${OBS_H}
OutputCX=${OBS_W}
OutputCY=${OBS_H}
FPSType=Common FPS Values
FPSCommon=60
ScaleType=bicubic

[Audio]
SampleRate=48000
ChannelSetup=Stereo

[Output]
Mode=Advanced

[AdvOut]
TrackIndex=1
VodTrackIndex=2

[NDI]
MainOutputName=HostDesktop
MainOutputEnabled=true
MainOutputBandwidth=highest
MainOutputAudioEnabled=true
ENDINI

# --- Start OBS with the NDI profile (informational) --------------------------

info ""
info "============================================================"
info "  Host NDI setup complete!"
info "============================================================"
info ""
info "  To start sharing your desktop via NDI:"
info ""
info "    1. Launch OBS with the NDI profile:"
info "         obs --profile NDI_ScreenShare --collection NDI_ScreenShare"
info ""
info "    2. OBS will start capturing your desktop and publishing an"
info "       NDI source named \"HostDesktop\" on the local network."
info ""
info "    3. On your Windows VM guest, run:"
info "         setup_guest_ndi.ps1"
info ""
info "    4. In Teams, select \"NDI Webcam Input\" as your camera."
info ""
info "  To control OBS programmatically, use ndi_control.py:"
info "    python ndi_control.py start"
info "    python ndi_control.py status"
info "    python ndi_control.py stop"
info ""
info "  Network: Ensure the VM uses bridged networking (not NAT) so"
info "  it can discover the NDI source via mDNS. If using NAT, set"
info "  the NDI Webcam Input source IP manually to the host's bridge IP."
info "============================================================"
