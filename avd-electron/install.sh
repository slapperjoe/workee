#!/usr/bin/env bash
set -e

APP_NAME="Workee"
APP_ID="workee"
INSTALL_DIR="${1:-$HOME/.local/share/$APP_ID}"
BIN_LINK_DIR="${2:-$HOME/.local/bin}"
DESKTOP_DIR="${3:-$HOME/.local/share/applications}"

echo "==> Building app (if needed)"
if [ ! -d "dist/${APP_NAME}-linux-x64" ]; then
  npx electron-packager . "$APP_NAME" --platform=linux --arch=x64 --out=dist --overwrite --icon=icon.png --app-version=1.0.0
  echo "    build complete"
else
  echo "    dist/${APP_NAME}-linux-x64 already exists, using it"
fi

BUILT="dist/${APP_NAME}-linux-x64"

echo "==> Installing to $INSTALL_DIR"
if [ -d "$INSTALL_DIR" ]; then
  echo "    removing existing install at $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
fi
mkdir -p "$(dirname "$INSTALL_DIR")"
cp -r "$BUILT" "$INSTALL_DIR"

# Place the app icon where the .desktop Icon= entry points to.
# (electron-packager's --icon is a no-op on Linux; the launcher
# resolves icons from the .desktop file, never from the ELF.)
if [ -f "icon.png" ]; then
  mkdir -p "$INSTALL_DIR/resources"
  cp "icon.png" "$INSTALL_DIR/resources/icon.png"
fi

echo "==> Creating symlink in $BIN_LINK_DIR"
mkdir -p "$BIN_LINK_DIR"
ln -sf "$INSTALL_DIR/${APP_NAME}" "$BIN_LINK_DIR/${APP_ID}"

# Install a hicolor icon set so the launcher can also resolve
# Icon=workee by theme name (covers compositor/desktop that prefer
# the theme lookup over an absolute path).
#
# The hicolor set must land in the *target user's* home, not the
# invoking shell's $HOME (a cross-user/system install would otherwise
# drop the icons where the target user's launcher never looks). Derive
# the target home from INSTALL_DIR when it follows the default
# $HOME/.local/share/$APP_ID layout, else fall back to $HOME.
ICON_ROOT=""
case "$INSTALL_DIR" in
  */.local/share/$APP_ID)
    ICON_ROOT="${INSTALL_DIR%/.local/share/$APP_ID}/.local/share/icons"
    ;;
  *)
    ICON_ROOT="${HOME:-/root}/.local/share/icons"
    ;;
esac

if [ -f "icon.png" ]; then
  install_icon_sizes=(16 32 48 64 128 256 512)
  hc_dirs=()
  for size in "${install_icon_sizes[@]}"; do
    if [ "$size" -le 256 ]; then
      dest_dir="$ICON_ROOT/hicolor/${size}x${size}/apps"
      hc_dirs+=("${size}x${size}/apps")
    else
      dest_dir="$ICON_ROOT/hicolor/scalable/apps"
      hc_dirs+=("scalable/apps")
    fi
    mkdir -p "$dest_dir"
    cp "icon.png" "$dest_dir/${APP_ID}.png"
  done

  # An icon theme engine (Qt/GTK/QuickShell) will not treat a hicolor
  # directory as a valid theme without an index.theme — the PNGs sit on
  # disk but Icon=workee still resolves to a missing-icon placeholder.
  # Generate one that declares exactly the directories we just created.
  {
    echo "[Icon Theme]"
    echo "Name=Hicolor"
    echo "Comment=Fallback icon theme"
    echo "Inherits="
    # de-dupe while preserving order
    printf '%s\n' "${hc_dirs[@]}" | awk '!seen[$0]++' > /tmp/.hcdirs.$$
    echo -n "Directories="
    paste -sd, /tmp/.hcdirs.$$
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      case "$d" in
        scalable/apps)
          echo ""
          echo "[scalable/apps]"
          echo "Size=256"
          echo "MinSize=256"
          echo "MaxSize=512"
          echo "Type=Scalable"
          ;;
        *)
          base="${d%/apps}"; sz="${base#*/}"
          echo ""
          echo "[$d]"
          echo "Size=$sz"
          echo "MinSize=$sz"
          echo "MaxSize=$sz"
          ;;
      esac
    done < /tmp/.hcdirs.$$
    rm -f /tmp/.hcdirs.$$
  } > "$ICON_ROOT/hicolor/index.theme"
  echo "    hicolor icon set installed to $ICON_ROOT/hicolor (index.theme written)"
fi

echo "==> Creating .desktop launcher in $DESKTOP_DIR"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/${APP_ID}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Exec=$INSTALL_DIR/${APP_NAME}
Icon=workee
Terminal=false
Categories=Utility;
EOF

chmod +x "$DESKTOP_DIR/${APP_ID}.desktop"

echo "==> Done."
echo "    Launch:   $BIN_LINK_DIR/${APP_ID}"
echo "    Desktop:  $DESKTOP_DIR/${APP_ID}.desktop"
echo "    Installed at: $INSTALL_DIR"
echo ""
echo "    For a system-wide install (all users), run:"
echo "      sudo ./install.sh /opt/$APP_ID /usr/local/bin /usr/share/applications"
