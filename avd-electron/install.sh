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
if [ -f "icon.png" ]; then
  ICON_ROOT="${HOME:-/root}/.local/share/icons"
  install_icon_sizes=(16 32 48 64 128 256 512)
  for size in "${install_icon_sizes[@]}"; do
    if [ "$size" -le 256 ]; then
      dest_dir="$ICON_ROOT/hicolor/${size}x${size}/apps"
      mkdir -p "$dest_dir"
      cp "icon.png" "$dest_dir/${APP_ID}.png"
    else
      dest_dir="$ICON_ROOT/hicolor/scalable/apps"
      mkdir -p "$dest_dir"
      cp "icon.png" "$dest_dir/${APP_ID}.png"
    fi
  done
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
