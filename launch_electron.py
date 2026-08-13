#!/usr/bin/env python3
"""Launch Electron AVD Dashboard."""
import os
import subprocess
import sys

ELECTRON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avd-electron")

env = os.environ.copy()
env.setdefault("GDK_BACKEND", "x11")

subprocess.run(["npx", "electron", "."], cwd=ELECTRON_DIR, env=env)
