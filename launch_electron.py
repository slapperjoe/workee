#!/usr/bin/env python3
"""Launch Electron AVD Dashboard."""
import glob
import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ELECTRON_DIR = os.path.join(BASE_DIR, "avd-electron")


def find_node():
    node = shutil.which("node")
    if node:
        return node
    candidates = [
        os.path.expanduser("~/.local/share/mise/shims/node"),
        os.path.expanduser("~/.local/bin/node"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    installs = sorted(
        glob.glob(os.path.expanduser("~/.local/share/mise/installs/node/*/bin/node")),
        reverse=True,
    )
    for path in installs:
        if os.path.isfile(path):
            return path
    return None


def main():
    env = os.environ.copy()
    env.setdefault("GDK_BACKEND", "x11")

    node = find_node()
    electron_cli = os.path.join(ELECTRON_DIR, "node_modules", "electron", "cli.js")

    if node and os.path.isfile(electron_cli):
        subprocess.run([node, electron_cli, "."], cwd=ELECTRON_DIR, env=env)
        return

    npx = shutil.which("npx") or (node and os.path.join(os.path.dirname(node), "npx"))
    if npx:
        subprocess.run([npx, "electron", "."], cwd=ELECTRON_DIR, env=env)
        return

    print("Could not locate node/npx. Install Node.js and run `npm install` in avd-electron.",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
