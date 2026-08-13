#!/usr/bin/env python3
"""Debug: check what the AVD entry page actually renders and whether JS redirects fire."""
import sys, time

from playwright.sync_api import sync_playwright

from avd_client import AVD_ENTRY_URL, CHROMIUM_ARGS, EDGE_UA


def main():
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
    ctx = browser.new_context(
        user_agent=EDGE_UA,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = ctx.new_page()
    page.set_default_timeout(15_000)

    try:
        print("Navigating to AVD entry...")
        page.goto(AVD_ENTRY_URL, wait_until="domcontentloaded")
        print(f"URL after domcontentloaded: {page.url}")

        # Wait for potential JS redirect
        print("Waiting 8s for JS redirect...")
        start = time.time()
        for i in range(8):
            time.sleep(1)
            url = page.url
            if "login" in url.lower():
                print(f"  ✓ Redirected to login at t={time.time()-start:.0f}s")
                break
            if i % 2 == 0:
                print(f"  t={time.time()-start:.0f}s: {url[:100]}")

        final_url = page.url
        print(f"\nFinal URL: {final_url}")

        if "login.microsoftonline.com" in final_url:
            print("\n--- Login page DOM ---")
            title = page.title()
            print(f"Title: {title}")
            # Check key elements
            inputs = page.query_selector_all("input")
            print(f"Input fields found: {len(inputs)}")
            for inp in inputs[:10]:
                t = inp.get_attribute("type") or "text"
                n = inp.get_attribute("name") or ""
                p = inp.get_attribute("placeholder") or ""
                vid = "visible" if inp.is_visible() else "hidden"
                print(f"  [{vid}] type={t} name={n} placeholder={p}")

            buttons = page.query_selector_all("button, input[type='submit']")
            print(f"Buttons/submits found: {len(buttons)}")
            for btn in buttons[:5]:
                txt = btn.inner_text().strip()[:50]
                vid = "visible" if btn.is_visible() else "hidden"
                print(f"  [{vid}] text={txt}")

        elif "windows.cloud.microsoft" in final_url:
            print("\nStill on AVD page. Checking if it's a loading state or full dashboard...")
            body_text = page.inner_text("body")[:500]
            print(f"Body text (first 500):\n{body_text}")

            # Check for JS errors
            page.on("console", lambda msg: None)  # no-op
            js_ok = page.evaluate("() => typeof window !== 'undefined'")
            print(f"JS running: {js_ok}")

            # Check if there's a sign-in link/button
            links = page.query_selector_all("a")
            signin_links = [l for l in links if "sign" in (l.inner_text().lower())]
            print(f"Sign-in links: {len(signin_links)}")

            # Check service worker / redirect logic
            spaurl = page.evaluate("() => window.location.href")
            print(f"window.location: {spaurl[:200]}")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback; traceback.print_exc()
    finally:
        ctx.close()
        browser.close()
        p.stop()


if __name__ == "__main__":
    main()
