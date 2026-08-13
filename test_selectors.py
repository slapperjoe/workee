#!/usr/bin/env python3
"""Real-world selector validation against Microsoft login page."""
import sys
import time

from playwright.sync_api import sync_playwright

from avd_client import AVD_ENTRY_URL, CHROMIUM_ARGS, EDGE_UA, SELECTORS


def check_selectors(page, label, selectors):
    """Check selectors for the manual live-inspection script.

    The ``check_`` prefix prevents pytest from collecting this helper as a
    test and looking for nonexistent fixtures.
    """
    results = []
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            visible = el.is_visible() if el else False
            results.append((sel, bool(el), visible))
        except Exception as e:
            results.append((sel, False, False))
    return results


def main():
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
    ctx = browser.new_context(user_agent=EDGE_UA, viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    page.set_default_timeout(30_000)

    try:
        print("Navigating to AVD entry page...")
        page.goto(AVD_ENTRY_URL, wait_until="domcontentloaded")
        time.sleep(3)  # Let redirects settle

        url = page.url
        print(f"Final URL: {url}")

        if "login.microsoftonline.com" in url:
            print("\n✓ Redirected to Microsoft login page\n")

            # Test email page selectors
            print("--- Email page selectors ---")
            for sel, found, vis in check_selectors(
                page, "email",
                [SELECTORS["email_input"], SELECTORS["email_input_fallback"],
                 SELECTORS["next_button_fallback"], SELECTORS["next_button"]],
            ):
                status = "✓ VISIBLE" if vis else ("✓ found (hidden)" if found else "✗ MISSING")
                print(f"  {status}: {sel}")

            # Fill email to advance to password page
            print("\nFilling email field to advance...")
            email_input = page.query_selector(SELECTORS["email_input"])
            if not email_input:
                email_input = page.query_selector(SELECTORS["email_input_fallback"])

            if email_input and email_input.is_visible():
                email_input.fill("testuser@example.com")
                print("  Filled email")

                next_btn = page.query_selector(SELECTORS["next_button_fallback"])
                if not next_btn:
                    next_btn = page.query_selector(SELECTORS["next_button"])
                if next_btn and next_btn.is_visible():
                    next_btn.click()
                    print("  Clicked Next")
                    time.sleep(3)

                    url2 = page.url
                    print(f"  URL after Next: {url2[:120]}")

                    # Test password page selectors
                    print("\n--- Password page selectors ---")
                    for sel, found, vis in check_selectors(
                        page, "password",
                        [SELECTORS["password_input"], SELECTORS["password_input_fallback"],
                         SELECTORS["signin_button_fallback"], SELECTORS["signin_button"]],
                    ):
                        status = "✓ VISIBLE" if vis else ("✓ found (hidden)" if found else "✗ MISSING")
                        print(f"  {status}: {sel}")

                    # Also test MFA detection (should be False on the password page)
                    print("\n--- MFA detection (should be False on password page) ---")
                    for sel in SELECTORS["mfa_indicators"]:
                        if sel.startswith('text="'):
                            text_val = sel[6:-1]
                            loc = page.get_by_text(text_val, exact=False)
                            found = loc.count() > 0
                        else:
                            el = page.query_selector(sel)
                            found = bool(el and el.is_visible())
                        if found:
                            print(f"  ⚠ UNEXPECTED MFA match: {sel}")
                    print("  (no unexpected MFA matches = good)")
                else:
                    print("  ✗ Next button not found")
            else:
                print("  ✗ Email input not found")
        elif "windows.cloud.microsoft" in url:
            print("\n✓ Already on AVD dashboard (possibly cached session)")
        else:
            print(f"\nUnexpected redirect target: {url[:200]}")

        # Summary
        print("\n=== Selector validation complete ===")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        ctx.close()
        browser.close()
        p.stop()


if __name__ == "__main__":
    main()
