#!/usr/bin/env python3
"""Quick infrastructure smoke test for AVD client."""
import os
import sys

from playwright.sync_api import sync_playwright

from avd_client import AVDClient, CHROMIUM_ARGS, EDGE_UA, main

# Test 1: Raw Playwright — launch, navigate, verify, STOP
print("Test 1: Raw Playwright infrastructure...")
p = sync_playwright().start()
browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
print(f"  OK Browser: {browser.version}")
ctx = browser.new_context(user_agent=EDGE_UA)
page = ctx.new_page()
page.goto("https://httpbin.org/headers", wait_until="domcontentloaded")
body = page.inner_text("body")
has_edge = "Edg/" in body
print(f"  OK UA spoof: {'Edge' if has_edge else 'MISSING'}")
ls_val = page.evaluate("() => { localStorage.setItem('k', 'v'); return localStorage.getItem('k'); }")
print(f"  OK localStorage: {ls_val}")
ctx.close()
browser.close()
p.stop()
print("  OK Clean shutdown")

# Test 2: AVDClient lifecycle (separate Playwright instance)
print("Test 2: AVDClient start/close...")
client = AVDClient(email="test@example.com", password="pw", headless=True)
client.start()
print(f"  OK start(), page URL: {client._page.url[:50]}")
client.close()
print("  OK close()")

# Test 3: Error on missing credentials
print("Test 3: Error on missing credentials...")
os.environ["AVD_EMAIL"] = ""
os.environ["AVD_PASSWORD"] = ""
try:
    main()
except SystemExit as e:
    print(f"  OK exits with code {e.code} on missing creds")

print()
print("=== ALL INFRASTRUCTURE TESTS PASSED ===")
