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

echo "==> Creating symlink in $BIN_LINK_DIR"
mkdir -p "$BIN_LINK_DIR"
ln -sf "$INSTALL_DIR/${APP_NAME}" "$BIN_LINK_DIR/${APP_ID}"

echo "==> Creating .desktop launcher in $DESKTOP_DIR"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/${APP_ID}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Exec=$INSTALL_DIR/${APP_NAME}
Icon=$INSTALL_DIR/resources/icon.png
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
