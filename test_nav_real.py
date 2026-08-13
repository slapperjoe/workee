#!/usr/bin/env python3
"""Debug: navigate directly to the login page to validate selectors."""
import sys, time

from playwright.sync_api import sync_playwright

from avd_client import AVD_ENTRY_URL, CHROMIUM_ARGS, EDGE_UA, SELECTORS, LOGIN_AUTHORITY


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
        # Navigate to AVD entry which should redirect us
        print("1. Navigating to AVD entry...")
        page.goto(AVD_ENTRY_URL, wait_until="domcontentloaded")
        time.sleep(2)
        real_url = page.evaluate("() => window.location.href")
        print(f"   Real URL: {real_url[:150]}")

        # If we're on the SPA sign-in page, click the "Sign in" button
        signin_btn = page.query_selector("text=Sign in")
        if signin_btn and signin_btn.is_visible() and "login" not in real_url.lower():
            print("   Clicking 'Sign in' button...")
            signin_btn.click()
            time.sleep(5)
            real_url = page.evaluate("() => window.location.href")
            print(f"   After click, real URL: {real_url[:150]}")

        # Now check what page we're actually on
        page_url = page.url
        real_url = page.evaluate("() => window.location.href")
        print(f"\n   Playwright URL: {page_url[:150]}")
        print(f"   Window location: {real_url[:150]}")

        # If still not on login page, navigate directly
        if "login.microsoftonline.com" not in real_url:
            print("\n2. Navigating directly to login page...")
            page.goto(f"{LOGIN_AUTHORITY}/common/oauth2/v2.0/authorize?"
                      f"client_id=451f2815-40fe-44bb-b8a6-3a2e55cf40c4",
                      wait_until="domcontentloaded")
            time.sleep(3)
            real_url = page.evaluate("() => window.location.href")
            print(f"   Real URL: {real_url[:150]}")

        # Now we should be on the login page — test selectors
        current_url = page.evaluate("() => window.location.href")
        print(f"\n3. Current page: {current_url[:150]}")

        if "login.microsoftonline.com" in current_url:
            print("\n✓ On Microsoft login page\n")

            # Test email selectors
            print("--- Email page selectors ---")
            tests = [
                ("email_input", SELECTORS["email_input"]),
                ("email_input_fb", SELECTORS["email_input_fallback"]),
                ("next_button", SELECTORS["next_button"]),
                ("next_button_fb", SELECTORS["next_button_fallback"]),
            ]
            for name, sel in tests:
                el = page.query_selector(sel)
                vis = el.is_visible() if el else False
                found = bool(el)
                print(f"  {'OK' if vis else ('found' if found else '--')} {name}: {sel}")

            # Try filling email
            print("\n--- Advancing to password page ---")
            email_input = page.query_selector(SELECTORS["email_input"])
            if not email_input:
                email_input = page.query_selector(SELECTORS["email_input_fallback"])

            if email_input and email_input.is_visible():
                email_input.fill("testuser@contoso.com")
                print("  Filled email")
                time.sleep(1)

                next_btn = page.query_selector(SELECTORS["next_button_fallback"])
                if not next_btn:
                    next_btn = page.query_selector(SELECTORS["next_button"])
                if next_btn:
                    next_btn.click()
                    print("  Clicked Next")
                    time.sleep(5)
                    new_url = page.evaluate("() => window.location.href")
                    print(f"  New URL: {new_url[:150]}")

                    # Password page selectors
                    print("\n--- Password page selectors ---")
                    pw_tests = [
                        ("password_input", SELECTORS["password_input"]),
                        ("password_input_fb", SELECTORS["password_input_fallback"]),
                        ("signin_button", SELECTORS["signin_button"]),
                        ("signin_button_fb", SELECTORS["signin_button_fallback"]),
                    ]
                    for name, sel in pw_tests:
                        el = page.query_selector(sel)
                        vis = el.is_visible() if el else False
                        found = bool(el)
                        print(f"  {'OK' if vis else ('found' if found else '--')} {name}: {sel}")

                    # MFA detection test (should be negative here)
                    print("\n--- MFA indicator check (should all be False) ---")
                    mfa_found = False
                    for sel in SELECTORS["mfa_indicators"]:
                        if sel.startswith('text="'):
                            text_val = sel[6:-1]
                            loc = page.get_by_text(text_val, exact=False)
                            if loc.count() > 0:
                                print(f"  UNEXPECTED: {sel}")
                                mfa_found = True
                        else:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                print(f"  UNEXPECTED: {sel}")
                                mfa_found = True
                    if not mfa_found:
                        print("  OK No MFA indicators found on password page")

                    # Fill password to see what happens
                    print("\n--- Submitting password ---")
                    pw_input = page.query_selector(SELECTORS["password_input"])
                    if not pw_input:
                        pw_input = page.query_selector(SELECTORS["password_input_fallback"])
                    if pw_input and pw_input.is_visible():
                        pw_input.fill("WrongPassword123!")
                        signin = page.query_selector(SELECTORS["signin_button_fallback"])
                        if not signin:
                            signin = page.query_selector(SELECTORS["signin_button"])
                        if signin:
                            signin.click()
                            print("  Clicked Sign in")
                            time.sleep(5)
                            after_pw_url = page.evaluate("() => window.location.href")
                            print(f"  URL after password: {after_pw_url[:200]}")

                            # Check for error message
                            error_el = page.query_selector(SELECTORS["password_error"])
                            if error_el and error_el.is_visible():
                                print(f"  Password error: {error_el.inner_text().strip()[:100]}")
                            else:
                                print("  No password error element visible")

                            # Check for MFA
                            mfa_now = False
                            for sel in SELECTORS["mfa_indicators"]:
                                if sel.startswith('text="'):
                                    text_val = sel[6:-1]
                                    loc = page.get_by_text(text_val, exact=False)
                                    if loc.count() > 0:
                                        if loc.first.is_visible():
                                            print(f"  MFA detected: {sel}")
                                            mfa_now = True
                                else:
                                    el = page.query_selector(sel)
                                    if el and el.is_visible():
                                        print(f"  MFA detected: {sel}")
                                        mfa_now = True
                            if not mfa_now:
                                print("  No MFA detected (expected with wrong password)")
                else:
                    print("  ✗ Next button not found")
            else:
                print("  ✗ Email input not found/visible")
        else:
            print(f"\nNot on login page. Page content preview:")
            try:
                body = page.inner_text("body")[:400]
                print(body)
            except:
                pass

        print("\n=== Done ===")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback; traceback.print_exc()
    finally:
        ctx.close()
        browser.close()
        p.stop()


if __name__ == "__main__":
    main()
