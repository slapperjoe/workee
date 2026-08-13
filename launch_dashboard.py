#!/usr/bin/env python3
"""Convenience launcher — starts the Tauri AVD Dashboard."""
import os
import subprocess
import sys

TAURI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avd-tauri")

# Resolve cargo binary
cargo = None
for p in [
    "/mnt/steam/data/hermes/home/.cargo/bin/cargo",
    os.path.expanduser("~/.cargo/bin/cargo"),
]:
    if os.path.isfile(p):
        cargo = p
        break

if not cargo:
    # Try PATH as last resort
    import shutil
    cargo = shutil.which("cargo")

if not cargo:
    print("ERROR: cargo not found", file=sys.stderr)
    print("Install Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh", file=sys.stderr)
    sys.exit(1)

env = os.environ.copy()
env["PATH"] = os.path.dirname(cargo) + ":" + env.get("PATH", "")
env.setdefault("GDK_BACKEND", "x11")
env.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

os.chdir(TAURI_DIR)
subprocess.run([cargo, "tauri", "dev"], env=env)
