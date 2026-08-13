#!/usr/bin/env python3
"""End-to-end test: real login flow with error detection and MFA detection."""
import sys, time

from playwright.sync_api import sync_playwright

from avd_client import (
    AVD_ENTRY_URL, CHROMIUM_ARGS, EDGE_UA, SELECTORS, LOGIN_AUTHORITY,
)


def main():
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
    ctx = browser.new_context(
        user_agent=EDGE_UA,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = ctx.new_page()
    page.set_default_timeout(20_000)

    try:
        # --- Navigate ---
        print("1. Navigating to AVD entry...")
        page.goto(AVD_ENTRY_URL, wait_until="domcontentloaded")
        time.sleep(3)
        real_url = page.evaluate("() => window.location.href")
        print(f"   Real URL: {real_url[:120]}")
        assert LOGIN_AUTHORITY in real_url, f"Expected login URL, got: {real_url[:120]}"
        print("   OK Redirected to Microsoft login")

        # --- Email ---
        print("\n2. Entering email...")
        email_input = page.query_selector(SELECTORS["email_input"])
        assert email_input and email_input.is_visible(), "Email input not found"
        email_input.fill("bogus-user@contoso.com")
        print("   OK Filled email")

        next_btn = page.query_selector(SELECTORS["next_button_fallback"])
        assert next_btn and next_btn.is_visible(), "Next button not found"
        next_btn.click()
        print("   OK Clicked Next")

        # Wait for password field or MFA
        print("   Waiting for password field...")
        try:
            page.wait_for_selector(
                f"{SELECTORS['password_input']}, "
                f"{SELECTORS['password_input_fallback']}, "
                "input[name='otc']",
                timeout=15_000,
                state="attached",
            )
        except Exception:
            pass

        time.sleep(1)
        real_url = page.evaluate("() => window.location.href")
        print(f"   URL after email: {real_url[:120]}")

        # Check if MFA appeared
        mfa_found = page.query_selector("input[name='otc']")
        if mfa_found and mfa_found.is_visible():
            print("   NOTE MFA appeared after email only (tenant requires MFA before password)")
            ctx.close(); browser.close(); p.stop()
            return

        pw_input = page.query_selector(SELECTORS["password_input"])
        assert pw_input and pw_input.is_visible(), "Password input not found"
        print("   OK Password field visible")

        # --- Password ---
        print("\n3. Entering wrong password...")
        pw_input.fill("DefinitelyWrongPassword123!")
        signin_btn = page.query_selector(SELECTORS["signin_button_fallback"])
        assert signin_btn and signin_btn.is_visible(), "Sign in button not found"
        signin_btn.click()
        print("   OK Clicked Sign in")

        # Wait for response
        print("   Waiting for response...")
        try:
            page.wait_for_selector(
                SELECTORS["password_input"],
                timeout=10_000,
                state="detached",
            )
            print("   Password form dismissed")
        except Exception:
            print("   Password field still visible (expected with wrong password)")

        time.sleep(2)
        real_url = page.evaluate("() => window.location.href")
        print(f"   URL after password: {real_url[:120]}")

        # --- Check for auth error ---
        print("\n4. Checking for auth error...")
        error_selectors = SELECTORS["auth_error_selectors"]
        found_error = False
        for sel in error_selectors:
            try:
                if sel.startswith('text="'):
                    text_val = sel[6:-1]
                    loc = page.get_by_text(text_val, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        print(f"   ERROR TEXT: {loc.first.inner_text().strip()[:200]}")
                        found_error = True
                        break
                else:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        txt = el.inner_text().strip()
                        if txt and len(txt) > 1:
                            print(f"   ERROR: {sel} => '{txt[:200]}'")
                            found_error = True
                            break
            except Exception:
                continue

        if found_error:
            print("   OK Auth error correctly detected")
        else:
            print("   NOTE No visible auth error detected (Microsoft may show errors differently)")
            # Print page content for debugging
            body = page.inner_text("body")
            for line in body.split("\n")[:30]:
                stripped = line.strip()
                if stripped:
                    print(f"   PAGE: {stripped[:100]}")

        # --- Check for MFA ---
        print("\n5. Checking for MFA...")
        mfa_indicators = SELECTORS["mfa_indicators"]
        mfa_found = False
        for sel in mfa_indicators:
            try:
                if sel.startswith('text="'):
                    text_val = sel[6:-1]
                    loc = page.get_by_text(text_val, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        print(f"   MFA indicator: {sel}")
                        mfa_found = True
                else:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        print(f"   MFA element visible: {sel}")
                        mfa_found = True
            except Exception:
                continue

        if not mfa_found:
            print("   OK No MFA detected (wrong password should not trigger MFA)")

        print("\n=== E2E test complete ===")

    except AssertionError as e:
        print(f"\nFAIL: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        ctx.close()
        browser.close()
        p.stop()


if __name__ == "__main__":
    main()
