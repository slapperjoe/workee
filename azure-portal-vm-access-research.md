# Azure Portal VM Browser Console Flows & Chromium Automation Feasibility

## 1. Executive Summary

The Azure portal (`portal.azure.com`) provides multiple browser-based VM access methods — Bastion (RDP/SSH), Serial Console, and browser-based SSH — all reachable through a single Entra ID authenticated session. This document maps the full authentication flow, session management mechanics, VM access connection architectures, and evaluates Chromium automation viability via Playwright and Puppeteer. The companion document `avd-web-client-research.md` covers Azure Virtual Desktop specifically; this document focuses on the Azure portal itself and its infrastructure VM access paths.

**Key finding**: A "constant" VM list page IS achievable with a long-lived browser profile. Playwright's `launchPersistentContext()` with a `userDataDir` preserves cookies, localStorage, and MSAL token cache across runs, enabling multi-day session reuse. The primary constraint is the SPA refresh token lifetime (24 hours for MSAL.js SPA clients) and periodic MFA re-prompting under Conditional Access policies.

---

## 2. Azure Portal Authentication Flow

### 2.1 Entry Point

| Property | Value |
|---|---|
| Portal URL | `https://portal.azure.com/` |
| SPA framework | React-based Azure Portal Framework (Ibiza) |
| Auth library | MSAL.js v5.13.0 (`x-client-SKU: msal.js.browser`) |
| Auth endpoint | `https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize` |

### 2.2 OAuth2 Authorization Code + PKCE Flow

The portal initiates a redirect-based OAuth2 flow distinct from the AVD web client:

```
GET https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize
  ?client_id=c44b4083-3bb0-49c1-b47d-974e53cbdf3c
  &scope=https://management.core.windows.net//.default openid profile offline_access
  &redirect_uri=https://portal.azure.com/auth/login/
  &response_mode=fragment
  &response_type=code
  &code_challenge_method=S256
  &code_challenge=<PKCE_challenge>
  &x-client-SKU=msal.js.browser
  &x-client-VER=5.13.0
  &state=<base64_json>
  &nonce=<random>
  &client_info=1
  &site_id=501430
  &instance_aware=true
```

Key differences from the AVD web client:

| Parameter | Azure Portal | AVD Web Client |
|---|---|---|
| **client_id** | `c44b4083-3bb0-49c1-b47d-974e53cbdf3c` | `451f2815-40fe-44bb-b8a6-3a2e55cf40c4` |
| **scope** | `https://management.core.windows.net//.default` | `https://www.wvd.microsoft.com/User.Access` |
| **redirect_uri** | `https://portal.azure.com/auth/login/` | `https://windows.cloud.microsoft/spa-signin-oidc` |
| **tenant** | `/organizations/` (home-tenant pinned) | `/common/` (allows tenant discovery) |
| **instance_aware** | `true` | absent |

### 2.3 Login Page Elements

The Microsoft sign-in page at `login.microsoftonline.com` presents:

| Element | Type | Ref | Notes |
|---|---|---|---|
| "Sign in" heading | h1 | @e6 | Main heading |
| Email/phone input | textbox (required) | @e7 | Placeholder: "Enter your email, phone, or Skype." |
| "Next" button | button | @e10 | Submits email |
| "Sign-in options" | button | @e4 | Alternative auth methods (FIDO2, GitHub, etc.) |
| "Sign in with GitHub" | button | @e5 | External IdP option (tenant-configured) |
| "Can't access your account?" | link | @e9 | Account recovery |
| Terms of use / Privacy | links | @e1, @e2 | Footer links |

Initial cookies set: `AADSSO=NA|NoExtension`, `SSOCOOKIEPULLED=1`, `brcap=0`.

### 2.4 Authentication Steps (User Journey)

1. **Email entry**: User enters email → tenant-specific redirect (organizations tenant).
2. **Password entry**: After email validation, password prompt appears.
3. **MFA prompt** (if configured):
   - SMS code entry
   - Microsoft Authenticator push notification (polling)
   - Authenticator TOTP code (6-digit)
   - FIDO2/Windows Hello (via "Sign-in options")
4. **Stay signed in** (KMSI): Optional "Keep me signed in" prompt.
5. **Redirect back**: `https://portal.azure.com/auth/login/#code=<auth_code>&...`
6. **Token exchange**: MSAL.js exchanges code for tokens via POST to `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`.
7. **Token storage**: Tokens stored in browser `localStorage` and `sessionStorage` under MSAL cache keys.

### 2.5 Azure Portal Token Cache Keys (localStorage)

The portal SPA uses MSAL v5 cache format:
- `{homeAccountId}-login.windows.net-{tenant}` — account entity
- `{homeAccountId}-login.windows.net-idtoken-{clientId}-{tenant}-` — ID token
- `{homeAccountId}-login.windows.net-accesstoken-{clientId}-{tenant}-<scopes>` — access token
- `{homeAccountId}-login.windows.net-refreshtoken-{clientId}-{tenant}-` — refresh token

Additional portal-specific localStorage keys (observed):
- `msal.<hash>` — active account metadata
- `shell-progress` — portal shell state
- Various `fxs_*` keys — portal framework (Ibiza) extension cache

### 2.6 Entra ID Authentication Cookies

Critical cookies for session persistence (from Microsoft Entra documentation):

| Cookie | Type | Purpose |
|---|---|---|
| `ESTSAUTH` | Session | User session info for SSO. Transient. |
| `ESTSAUTHPERSISTENT` | Persistent | User session info for SSO. Survives browser close. |
| `ESTSAUTHLIGHT` | Session | Session GUID for OIDC sign-out. |
| `esctx` | Session | CSRF protection. Binds request to browser instance. |
| `SignInStateCookie` | Session | Tracks services accessed for sign-out. |
| `fpc` | Persistent | Browser fingerprint for throttling. |
| `buid` | Persistent | Browser ID for telemetry/protection. |
| `x-ms-gateway-slice` | Session | Gateway load balancing. |
| `stsservicecookie` | Session | Gateway tracking. |
| `x-ms-refreshtokencredential` | Session | Present when Primary Refresh Token (PRT) is in use. |

### 2.7 Token Lifetimes

| Token Type | Default Lifetime | Configurable? |
|---|---|---|
| Access token | 60–90 minutes (varies) | No (deprecated policy) |
| Refresh token (SPA) | 24 hours | No |
| Refresh token (non-SPA) | 90 days | No |
| Session token (non-persistent) | 24 hours max inactive | No |
| Session token (persistent) | 90 days max inactive | Extends on use |

**Implication for automation**: A Playwright persistent context using `ESTSAUTHPERSISTENT` cookie + refresh token can maintain a session for up to 24 hours before needing a full re-auth. In practice, periodic navigation to the portal (e.g., every 60 minutes) triggers silent token refresh, extending the session as long as the refresh token remains valid.

---

## 3. Azure Portal VM Access Methods

The Azure portal provides four browser-based VM access methods, each with distinct connection architecture:

### 3.1 Method Comparison

| Method | Protocol | Connection Transport | Requires Bastion? | Opens In |
|---|---|---|---|---|
| **Bastion RDP** | RDP over TLS | WebSocket (WSS port 443) | Yes (Basic+) | New browser tab (HTML5 client) |
| **Bastion SSH** | SSH over TLS | WebSocket (WSS port 443) | Yes (Basic+) | New browser tab (HTML5 terminal) |
| **Serial Console** | Serial (COM1) | WebSocket to `*.gateway.serialconsole.azure.com` | No | In-portal blade (embedded iframe) |
| **Browser-based SSH (direct)** | SSH | WebSocket (WSS port 443) via Bastion | Yes | New browser tab |

### 3.2 Azure Bastion Architecture

```
┌──────────┐     HTTPS/WSS       ┌──────────────┐     Internal VNet     ┌──────────┐
│  Browser │ ◄──────────────────► │ Azure Bastion │ ◄──────────────────► │    VM    │
│ (portal) │     (port 443)       │   Gateway     │   (RDP/SSH relay)    │          │
└──────────┘                      └──────────────┘                       └──────────┘
                                          │
                                   ┌──────┴──────┐
                                   │ Azure Front  │
                                   │    Door      │
                                   └─────────────┘
```

**Connection sequence for Bastion RDP:**
1. User navigates to VM in portal → Connect → Bastion
2. User selects RDP protocol, enters credentials (or uses Entra ID auth)
3. Clicks **Connect** → portal opens a **new browser tab**
4. The new tab loads the Bastion HTML5 web client
5. Client establishes a **WSS (WebSocket Secure)** connection to Bastion Gateway via Azure Front Door
6. Gateway validates the request → instructs the Bastion agent on the VM to connect to the **same gateway**
7. Gateway relays RDP traffic between browser WebSocket and VM's internal connection ("reverse connect transport")
8. RDP handshake completes over the nested TLS tunnel

**Bastion HTML5 client URL pattern:**
```
https://portal.azure.com/?websocketendpoint=<wss_uri>&...
```
Or the Bastion session opens as a dedicated sub-page. The exact URL is dynamically generated.

### 3.3 Serial Console Architecture

```
┌──────────┐      WSS       ┌──────────────────────────┐     Serial (COM1)     ┌──────────┐
│  Browser │ ◄─────────────► │ <region>.gateway.        │ ◄──────────────────► │    VM    │
│ (portal) │                │ serialconsole.azure.com   │                       │          │
└──────────┘                └──────────────────────────┘                       └──────────┘
```

**Key characteristics:**
- WebSocket to `<region>.gateway.serialconsole.azure.com`
- Connects to VM's COM1 serial port (works even when OS is unresponsive)
- Accessible only through Azure portal (no standalone URL)
- Requires **Contributor** or higher RBAC role on the VM
- Firewall must allow `*.serialconsole.azure.com`
- Dedicated US Gov endpoint: `serialconsole.azure.us`

**Serial Console UI:**
- Opens as an embedded blade/panel within the portal (NOT a new tab)
- Text-based terminal with SAC (Special Administration Console) for Windows
- GRUB/serial-getty console for Linux
- Supports NMI (Non-Maskable Interrupt) and SysRq commands

### 3.4 VM List Page (portal.azure.com)

**URL pattern (hash-fragment routing):**
```
https://portal.azure.com/#view/Microsoft_Azure_Compute/VirtualMachines
https://portal.azure.com/#browse/Microsoft.Compute/virtualMachines
```

**API backing the VM list:**
The portal calls Azure Resource Manager (ARM) REST APIs:
```
GET https://management.azure.com/subscriptions/{subscriptionId}/providers/Microsoft.Compute/virtualMachines?api-version=2026-03-02
Authorization: Bearer <access_token>
```

This is a standard ARM API call — no special portal-only endpoints. The same API can be called programmatically with a valid Bearer token extracted from the browser session.

**VM list page UI behavior:**
- Loads as a blade within the portal's Ibiza SPA framework
- VM rows are rendered dynamically from ARM API responses
- Each VM row has a "Connect" dropdown with Bastion, RDP, SSH, and Serial Console options
- Clicking a VM name navigates to the VM overview blade (same tab)
- Clicking "Connect → Bastion" opens the Bastion connection flow (eventually a new tab)

### 3.5 VM "Connect" Menu Options

When a VM is selected, the portal presents:

| Option | Protocol | Opens | Requires |
|---|---|---|---|
| **Connect via Bastion** | RDP or SSH | New browser tab | Bastion host deployed |
| **Connect via RDP** | Native RDP | Downloads .rdp file | Public IP or VPN |
| **Connect via SSH** | Native SSH | Shows SSH command | Public IP or VPN |
| **Serial Console** | Serial/COM1 | In-portal blade | RBAC: Contributor+ |

---

## 4. Chromium Automation Evaluation

### 4.1 Playwright vs Puppeteer Comparison

| Criterion | Playwright | Puppeteer |
|---|---|---|
| **Persistent sessions** | `launchPersistentContext(userDataDir)` — full browser profile persistence | `puppeteer.launch({userDataDir})` — same capability |
| **Multi-context support** | First-class BrowserContext API with isolated storage per context | Manual page/context management |
| **Multi-tab handling** | `context.on('page')` event + `context.pages()` | `browser.on('targetcreated')` + `browser.pages()` |
| **New tab detection** | `context.waitForEvent('page')` — clean async | `browser.waitForTarget()` — more verbose |
| **Session storage export** | `context.storageState(path=...)` — JSON file | Manual cookie + localStorage extraction |
| **UA spoofing** | Built-in `userAgent` per context | Built-in `userAgent` per page |
| **Chromium flags** | `args=[...]` in launch options | `args=[...]` in launch options |
| **Python bindings** | ✅ First-class (pip install playwright) | ⚠️ Third-party (pyppeteer — less maintained) |
| **Headless mode** | ✅ `headless=True` | ✅ `headless='new'` |
| **Network interception** | `page.route()` API | `page.setRequestInterception()` |
| **CDP session access** | `page.context.new_cdp_session(page)` | `page.createCDPSession()` |
| **Wayland/Linux support** | ✅ Mature | ✅ Mature |

**Recommendation: Playwright** for the following reasons:
- First-class Python bindings (matches existing project)
- `launchPersistentContext` + `storageState` provide two complementary session persistence strategies
- BrowserContext API maps naturally to "portal dashboard in main tab + VM sessions in sibling tabs"
- `context.waitForEvent('page')` handles Bastion's new-tab connection pattern cleanly
- Existing `avd_session.py` codebase already uses Playwright

### 4.2 Persistent Session Strategies

#### Strategy A: `launchPersistentContext` (Recommended)

Uses a persistent `userDataDir` that survives browser restarts. All browser state — cookies, localStorage, sessionStorage, IndexedDB, service workers — is preserved.

```python
from playwright.sync_api import sync_playwright

user_data_dir = "./azure-portal-profile"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=True,
        user_agent=EDGE_UA,
        viewport={"width": 1920, "height": 1080},
        args=[
            "--enable-features=SharedArrayBuffer",
            "--enable-features=CrossOriginOpenerPolicy",
        ],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://portal.azure.com/#browse/Microsoft.Compute/virtualMachines")
    # Session is already loaded from userDataDir if previously authenticated
```

**Pros:**
- Full browser profile persistence — no need to manually save/load state
- Survives browser process restarts
- MSAL.js token cache in localStorage is preserved automatically
- `ESTSAUTHPERSISTENT` cookie survives across launches

**Cons:**
- Larger disk footprint (~50-200MB per profile)
- Profile directory must be locked (single process at a time)
- Profile corruption possible on crash (mitigated with backup)

#### Strategy B: `storageState` Export/Import (Lightweight)

Export cookies + localStorage to a JSON file after login, re-import on subsequent runs.

```python
# First run: login, then save
context.storage_state(path="./auth-state.json")

# Subsequent runs: load saved state
context = browser.new_context(storage_state="./auth-state.json")
```

This is the approach already used by `avd_session.py`. Works well when:
- Only cookies + localStorage matter (not IndexedDB or service workers)
- Profile directory overhead is undesirable
- Multiple concurrent contexts from the same browser are needed

**Limitation**: Does NOT preserve IndexedDB. If the Azure portal stores critical session data in IndexedDB (unlikely but possible), this strategy won't capture it.

#### Strategy C: CDP `connectOverCDP` (Connect to Existing Browser)

Connect Playwright to an already-running, already-authenticated Chromium via the remote debugging port.

```bash
# Launch Chromium with debugging port
chromium --remote-debugging-port=9222 --user-data-dir=./azure-profile &

# In Playwright:
browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
```

**Best for**: Interactive development/debugging. Not recommended for production automation (requires manual browser lifecycle management).

### 4.3 Multi-Tab Handling for Bastion Sessions

When the Azure portal opens a Bastion session, it spawns a **new browser tab**. Playwright handles this natively:

```python
# Listen for new pages (tabs) opened by the portal
async with context.expect_page() as new_page_info:
    await page.click('button:has-text("Connect")')  # Bastion connect button

bastion_page = await new_page_info.value
await bastion_page.wait_for_load_state("domcontentloaded")
# Monitor the WebSocket connection on bastion_page
```

For synchronous API:

```python
with context.expect_page() as new_page:
    page.click('button:has-text("Connect")')

bastion_page = new_page.value
```

**Important**: The new tab inherits the browser context's cookies and localStorage (same context), so authentication is shared automatically. No re-login needed for the Bastion tab.

### 4.4 MFA Handling Strategies (Same as AVD)

Three approaches, ordered by suitability for headless automation:

| Approach | Automation-Friendly | Setup Required |
|---|---|---|
| **TOTP Secret** (pyotp) | ✅ Fully automated | Configure Microsoft Authenticator to expose TOTP secret |
| **Session Reuse** | ✅ No MFA needed after initial login | Save storage state; re-auth only when refresh token expires |
| **Manual Wait** | ⚠️ Requires human intervention | Prompt operator for code via stdin |

**Recommendation**: Use **Session Reuse** for daily operation, with **TOTP** as fallback when the refresh token expires. The initial login + MFA is a one-time cost; subsequent runs use the saved session.

### 4.5 Required Chromium Flags

For Bastion RDP WebAssembly support (same requirements as AVD):

```python
CHROMIUM_ARGS = [
    "--enable-features=SharedArrayBuffer",       # Required for RDP WebAssembly codec
    "--enable-features=CrossOriginOpenerPolicy", # Required for SharedArrayBuffer isolation
    "--enable-features=VaapiVideoDecoder",       # Hardware H.264 decode for RDP graphics
]
```

For Serial Console (text-based), these flags are NOT required. The serial console is a simple WebSocket terminal — no WebAssembly or video decode needed.

### 4.6 User-Agent Spoofing

Same Edge/Windows UA as the AVD client (Conditional Access policies may require it):

```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0
```

---

## 5. "Constant" VM List Page Feasibility

### 5.1 Can a VM list page stay open indefinitely?

**Yes, with caveats.** The Azure portal is a SPA that:
- Uses MSAL.js for token management with automatic silent refresh
- The VM list blade calls ARM APIs (`management.azure.com`) with Bearer tokens
- As long as the refresh token is valid, MSAL.js silently refreshes access tokens

**What breaks the session:**
1. **Refresh token expiry** (24 hours for SPA clients) — requires full re-auth
2. **Conditional Access session controls** — sign-in frequency policies force re-auth
3. **Portal session timeout** — the portal itself may enforce idle timeout (configurable in Entra ID)
4. **Browser/process crash** — mitigated with persistent profile
5. **Entra ID session revocation** — admin-initiated

### 5.2 Mitigation: Session Keep-Alive

A periodic "heartbeat" navigation keeps the session alive:

```python
import asyncio

async def keep_alive(page, interval_seconds=300):
    """Periodically refresh the VM list to trigger silent token refresh."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            current_url = page.url
            if "login.microsoftonline.com" in current_url:
                # Session expired — trigger re-auth
                await reauthenticate(page)
            else:
                # Reload the VM list to exercise the access token
                await page.reload()
        except Exception as e:
            logger.warning(f"Heartbeat failed: {e}")
```

**Recommended heartbeat interval**: 5–15 minutes. This is well within the 60–90 minute access token lifetime and keeps the MSAL.js library actively refreshing.

### 5.3 Architecture for a Persistent VM Dashboard

```
┌──────────────────────────────────────────────────────────┐
│                      Azure Dashboard Manager               │
│                                                           │
│  ┌──────────────┐     ┌──────────────────────────────┐   │
│  │ Auth Module   │     │       Session Monitor         │   │
│  │               │     │                              │   │
│  │ • Initial     │────▶│ • Heartbeat every 5m         │   │
│  │   login+MFA   │     │ • Detect login redirect      │   │
│  │ • TOTP fallback│    │ • Re-auth on expiry          │   │
│  │ • Save state  │     │ • Track re-auth count        │   │
│  └──────────────┘     └──────────────────────────────┘   │
│                                                           │
│  ┌──────────────────────────────────────────────────────┐ │
│  │              VM Operations                            │ │
│  │                                                       │ │
│  │  • List VMs (ARM API via page.evaluate fetch)         │ │
│  │  • Connect via Bastion (click in portal, new tab)     │ │
│  │  • Open Serial Console (click in portal, embedded)    │ │
│  │  • Monitor connection health                          │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                           │
│  ┌──────────────────────────────────────────────────────┐ │
│  │        Playwright Persistent Context                  │ │
│  │  • userDataDir = ./azure-portal-profile              │ │
│  │  • UA: Edge/Windows spoofed                          │ │
│  │  • Flags: SharedArrayBuffer, CrossOriginOpenerPolicy │ │
│  └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

### 5.4 Tab Management Strategy

| Tab | Purpose | Lifecycle |
|---|---|---|
| **Main dashboard tab** | VM list + portal navigation | Persistent — kept alive with heartbeat |
| **Bastion RDP tab** (N) | Active VM remote session | Created on demand when user connects to a VM |
| **Bastion SSH tab** (N) | Active VM SSH session | Created on demand |
| **Serial Console** | Debug/emergency console | Opens as embedded frame in portal, not a new tab |

Tab monitoring:
- Detect new tabs via `context.on('page')` / `context.waitForEvent('page')`
- Detect tab close (VM disconnect) via `page.on('close')`
- Maintain a map of VM name → tab/page reference

---

## 6. Key URLs & Identifiers

| Purpose | URL / ID |
|---|---|
| Azure Portal | `https://portal.azure.com/` |
| Portal VM list | `https://portal.azure.com/#browse/Microsoft.Compute/virtualMachines` |
| Portal auth redirect | `https://portal.azure.com/auth/login/` |
| Auth endpoint | `https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize` |
| Token endpoint | `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` |
| ARM VM list API | `https://management.azure.com/subscriptions/{id}/providers/Microsoft.Compute/virtualMachines?api-version=2026-03-02` |
| Portal client ID | `c44b4083-3bb0-49c1-b47d-974e53cbdf3c` |
| Portal API scope | `https://management.core.windows.net//.default` |
| Serial Console WS | `<region>.gateway.serialconsole.azure.com` |
| AVD web client | `https://windows.cloud.microsoft` |
| MFA security info | `https://mysignins.microsoft.com/security-info` |

---

## 7. Existing Codebase Integration

The `workee` project already implements the AVD web client path (`windows.cloud.microsoft`). This research covers the Azure portal path (`portal.azure.com`). The two paths share:

- **Authentication infrastructure**: Both use MSAL.js + Entra ID OAuth2 PKCE. The portal uses a different `client_id` and `scope`, but the login page interaction (email → password → MFA) is identical.
- **Session persistence**: `storageState` / persistent context patterns work for both.
- **Chromium flags**: Same `SharedArrayBuffer` + `CrossOriginOpenerPolicy` for RDP.
- **MFA handling**: Same TOTP/manual/session-reuse strategies.

**What's different:**
- Portal uses hash-fragment routing (`#browse/...`, `#view/...`); AVD uses standard paths (`/webclient/avd/<guid>`)
- Portal VM access goes through Bastion (separate service); AVD goes through AVD Gateway
- Portal serial console is portal-embedded; AVD has no serial console equivalent
- Portal API is ARM (`management.azure.com`); AVD API is feed discovery (`rdweb.wvd.microsoft.com`)

### Integration approach:

Extend `avd_session.py` to support an `--azure-portal` mode that:
1. Uses portal OAuth2 parameters (client_id, scope, redirect_uri)
2. Navigates to `portal.azure.com/#browse/Microsoft.Compute/virtualMachines` instead of AVD dashboard
3. Calls ARM API for VM listing instead of feed discovery
4. Opens Bastion tabs instead of AVD webclient tabs

Or, create a parallel `azure_portal_session.py` that mirrors the `AVDSessionManager` pattern for the portal path.

---

## 8. Automation Recommendations

### 8.1 Technology Stack

| Component | Recommendation | Rationale |
|---|---|---|
| Browser automation | **Playwright** (Python) | First-class persistent context, matches existing codebase, clean multi-tab handling |
| Session persistence | `launchPersistentContext(userDataDir)` | Survives restarts, zero manual state management |
| Fallback session persistence | `context.storageState(path)` | Lightweight, compatible with existing `auth-state.json` pattern |
| MFA handling | Session reuse (primary) + TOTP (fallback) | Minimizes human intervention |
| VM listing | ARM REST API via `page.evaluate(fetch)` | Faster and more reliable than DOM scraping; Bearer token already in browser |
| VM connection | Portal UI automation (click Connect → Bastion) | Bastion requires portal interaction; can't be done purely via API |

### 8.2 Implementation Phases

**Phase 1: Portal Auth & Session Persistence**
- Extend MFA/login logic to handle portal OAuth2 parameters
- Test `launchPersistentContext` session survival across browser restarts
- Verify token refresh over 24+ hours

**Phase 2: VM List & Monitoring**
- Extract access token from MSAL localStorage cache
- Call ARM VM list API programmatically
- Implement heartbeat/keep-alive loop
- Detect and handle session expiration

**Phase 3: Bastion Session Launch**
- Automate "Connect → Bastion" flow in portal
- Handle new-tab detection for Bastion HTML5 client
- Monitor WebSocket connection health
- Support multiple concurrent Bastion sessions

**Phase 4: Serial Console Access**
- Automate "Connect → Serial Console" flow
- Handle embedded console iframe/blade
- Send commands and read output via WebSocket

---

## 9. Known Limitations & Risks

| Limitation | Impact | Mitigation |
|---|---|---|
| SPA refresh token: 24h max | Session requires re-auth daily | TOTP automation for re-auth; alert on MFA prompt |
| Conditional Access sign-in frequency | May force re-auth more often | Respect tenant policy; detect and surface to operator |
| Bastion session in new tab | Requires multi-tab monitoring | Playwright context page events |
| Serial Console embedded in portal | Harder to isolate as a separate page | Use CDP to access iframe content |
| Portal SPA complexity (Ibiza framework) | DOM selectors brittle | Prefer ARM API calls over DOM scraping for data; use role/text selectors for clicks |
| Browser profile corruption | Session loss on crash | Backup userDataDir periodically |
| `launchPersistentContext` single-process lock | Can't run multiple instances against same profile | Use separate profiles per instance, or pool contexts from one browser |
| Entra ID session revocation | Admin can kill session at any time | Detect redirect to login; trigger re-auth |

---

## 10. References

- [Azure Bastion overview](https://learn.microsoft.com/en-us/azure/bastion/bastion-overview)
- [Connect to Windows VM via Bastion RDP](https://learn.microsoft.com/en-us/azure/bastion/bastion-connect-vm-rdp-windows)
- [Azure Serial Console errors](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/windows/serial-console-errors)
- [Entra ID authentication cookies](https://learn.microsoft.com/en-us/entra/identity/authentication/concept-authentication-web-browser-cookies)
- [Refresh token lifetimes](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens)
- [Configurable token lifetimes](https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes)
- [Playwright persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [AVD Web Client Research](./avd-web-client-research.md) (companion document)
