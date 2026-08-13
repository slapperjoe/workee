# Azure Chromium Wrapper — API Specification & Architecture

## 1. Overview

An async Python library that provides headless Chromium access to Azure VM
remote sessions. It authenticates against Microsoft Entra ID, navigates the
Azure portal or Azure Virtual Desktop (AVD) web client, and surfaces VM
connections through a clean programmatic API.

The wrapper supports two backend paths from a single session abstraction:

| Backend | Entry URL | VM List API | Connection Method |
|---|---|---|---|
| **AVD** | `windows.cloud.microsoft` | Feed discovery REST API | RDP WebAssembly in new tab |
| **Portal** | `portal.azure.com` | ARM REST API | Bastion (new tab) / Serial Console (embedded) |

Both paths share the same auth layer (Entra ID OAuth2 PKCE + MSAL.js) and MFA
handling, but differ in VM listing and connection mechanics.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      Caller (CLI / API consumer)              │
│                                                               │
│  session = PortalSession(config)                              │
│  await session.start()                                       │
│  if session.mfa_pending:           ◄── signal/wait pattern   │
│      code = prompt_user()                                    │
│      await session.provide_mfa_code(code)                    │
│  vms = await session.get_vms()                               │
│  conn = await session.connect(vms[0].id)                     │
│  await session.run_loop()          ◄── keep-alive heartbeat  │
└──────────────┬───────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│                      AzureSession (ABC)                       │
│                                                               │
│  ┌─────────────────┐  ┌──────────────────────────────────┐   │
│  │   AuthManager    │  │        SessionMonitor             │   │
│  │                  │  │                                   │   │
│  │ • OAuth2 PKCE    │  │ • Heartbeat (5–15 min interval)  │   │
│  │ • Email/password │  │ • Detect login redirect           │   │
│  │ • Token cache    │  │ • Trigger re-auth on expiry       │   │
│  │ • Session save   │  │ • Track re-auth count + uptime    │   │
│  └────────┬─────────┘  └──────────────────────────────────┘   │
│           │                                                    │
│  ┌────────▼─────────────────────────────────────────────────┐ │
│  │                    MfaManager                              │ │
│  │                                                            │ │
│  │  • detect(page) → MfaPromptType | None                    │ │
│  │  • handle_auto(page, method, secret) → MfaResult          │ │
│  │  • Signal/wait: mfa_event (asyncio.Event)                 │ │
│  │  • Support: TOTP, push notification polling, manual code  │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │          Playwright Persistent Context                     │  │
│  │                                                           │  │
│  │  Primary: launchPersistentContext(userDataDir)            │  │
│  │  Fallback: context.storageState(path) export/import       │  │
│  │  UA: Edge/Windows spoofed                                 │  │
│  │  Flags: SharedArrayBuffer, CrossOriginOpenerPolicy,       │  │
│  │         VaapiVideoDecoder                                 │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
               │
    ┌──────────┴──────────┐
    │                     │
┌───▼──────────┐  ┌──────▼──────────┐
│  AVDSession   │  │ PortalSession   │
│               │  │                 │
│ • Feed disc.  │  │ • ARM VM list   │
│ • AVD webcl.  │  │ • Bastion tabs  │
│   tabs        │  │ • Serial Console│
└───────────────┘  └─────────────────┘
```

---

## 3. Configuration

### 3.1 `AzureConfig`

All configuration lives in a single dataclass that can be populated from
environment variables, a `.env` file, or programmatically.

```python
@dataclass
class AzureConfig:
    # -- Credentials (required) --
    email: str                          # AVD_EMAIL
    password: str                       # AVD_PASSWORD

    # -- Backend selection --
    backend: Literal["avd", "portal"] = "portal"

    # -- MFA --
    mfa_method: Literal["totp", "manual", "auto"] = "auto"
    totp_secret: str | None = None      # AVD_TOTP_SECRET
    mfa_timeout: float = 120.0          # seconds to wait for MFA prompt
    mfa_post_submit_timeout: float = 60.0

    # -- Session persistence --
    persistence_mode: Literal["persistent_context", "storage_state"] = \
        "persistent_context"
    user_data_dir: str = "./azure-browser-profile"
    storage_state_path: str = "./auth-state.json"

    # -- Browser --
    headed: bool = False
    chromium_args: list[str] = field(default_factory=lambda: [
        "--enable-features=SharedArrayBuffer",
        "--enable-features=CrossOriginOpenerPolicy",
        "--enable-features=VaapiVideoDecoder",
    ])
    user_agent: str = EDGE_UA
    viewport: dict = field(default_factory=lambda: {"width": 1920, "height": 1080})

    # -- Session monitor --
    heartbeat_interval: float = 300.0   # seconds (5 min)
    reauth_timeout: float = 300.0       # max seconds for full re-auth
    session_poll_interval: float = 15.0 # seconds between expiry checks

    # -- Portal-specific --
    subscription_id: str | None = None  # Azure subscription filter

    @classmethod
    def from_env(cls) -> "AzureConfig":
        """Load from environment variables (.env already loaded by caller)."""
        ...
```

### 3.2 Environment variable mapping

| Variable | Field | Default |
|---|---|---|
| `AZURE_EMAIL` | `email` | (required) |
| `AZURE_PASSWORD` | `password` | (required) |
| `AZURE_BACKEND` | `backend` | `"portal"` |
| `AZURE_MFA_METHOD` | `mfa_method` | `"auto"` |
| `AZURE_TOTP_SECRET` | `totp_secret` | — |
| `AZURE_MFA_TIMEOUT` | `mfa_timeout` | `120` |
| `AZURE_PERSISTENCE` | `persistence_mode` | `"persistent_context"` |
| `AZURE_USER_DATA_DIR` | `user_data_dir` | `"./azure-browser-profile"` |
| `AZURE_STORAGE_STATE` | `storage_state_path` | `"./auth-state.json"` |
| `AZURE_HEADED` | `headed` | `false` |
| `AZURE_HEARTBEAT` | `heartbeat_interval` | `300` |
| `AZURE_SUBSCRIPTION_ID` | `subscription_id` | — |

Legacy `AVD_*` var names continue to work for backward compatibility.

---

## 4. Core API

### 4.1 `AzureSession` (abstract base)

```python
class AzureSession(ABC):
    """Persistent Chromium session for Azure VM remote access.

    Manages browser lifecycle, authentication, session keep-alive,
    VM listing, and VM connection. Backend-specific subclasses
    (AVDSession, PortalSession) implement the VM operations.
    """

    def __init__(self, config: AzureConfig): ...

    # -- Lifecycle --

    async def start(self) -> bool:
        """Launch browser, authenticate, navigate to VM list.

        Returns True if authentication succeeded (possibly after MFA).
        If MFA is needed and can't be resolved automatically, sets
        `mfa_pending = True` and returns True — the caller must then
        call provide_mfa_code() or provide_mfa_approval().
        """

    async def stop(self) -> None:
        """Save session state and close browser cleanly."""

    async def run_loop(self) -> None:
        """Block until interrupted, keeping the session alive.

        Periodically checks for session expiration and re-authenticates
        when needed. The heartbeat interval is configured via
        config.heartbeat_interval. Runs until stop() is called from
        another task or SIGINT/SIGTERM is received.
        """

    # -- Authentication --

    async def login(self, credentials: "Credentials | None" = None) -> bool:
        """Perform full login flow: email → password → MFA → VM list.

        If credentials is None, uses config.email / config.password.
        Handles MFA according to config.mfa_method.
        On MFA requiring user interaction, sets mfa_pending = True.
        """

    # -- MFA (signal/wait pattern) --

    @property
    def mfa_pending(self) -> bool:
        """True when MFA is required and waiting for user input."""

    @property
    def mfa_event(self) -> asyncio.Event:
        """Event that is set when MFA resolution is needed.

        The session sets this event when it detects an MFA prompt it
        cannot resolve automatically (no TOTP secret, push-only, etc.).
        The caller awaits this event, collects input from the user,
        then calls provide_mfa_code() or provide_mfa_approval().
        """

    async def provide_mfa_code(self, code: str) -> None:
        """Submit a user-provided MFA code to the login page.

        Call this after mfa_event is set and the user has provided
        a 6-digit code from their authenticator app or SMS.
        """

    async def provide_mfa_approval(self) -> None:
        """Signal that the user approved a push notification.

        For push-based MFA: call this to tell the session the user
        has approved on their device. The session will then poll
        for the push screen to disappear.
        """

    # -- VM operations (backend-specific) --

    @abstractmethod
    async def get_vms(self) -> list["VmInfo"]:
        """Return available VMs from the current backend."""

    @abstractmethod
    async def connect(self, vm_id: str) -> "VmConnection":
        """Open a remote session to the specified VM.

        For AVD: opens the RDP web client in a new browser tab.
        For Portal/Bastion: initiates Bastion connect, captures new tab.
        For Portal/SerialConsole: opens embedded console in the portal.

        Returns a VmConnection handle for monitoring the session.
        """

    # -- Status --

    async def get_state(self) -> "SessionState":
        """Return a snapshot of the session's current state."""

    # -- Internal (override in subclass) --

    @abstractmethod
    async def _get_entry_url(self) -> str: ...
    @abstractmethod
    async def _get_vm_list_url(self) -> str: ...
    @abstractmethod
    async def _is_on_vm_list(self, page) -> bool: ...
    @abstractmethod
    async def _fetch_vms(self, page) -> list["VmInfo"]: ...
    @abstractmethod
    async def _connect_vm(self, page, vm_id: str) -> "VmConnection": ...
```

### 4.2 `AVDSession` (AVD backend)

```python
class AVDSession(AzureSession):
    """Session targeting the Azure Virtual Desktop web client.

    VM listing via feed discovery API at rdweb.wvd.microsoft.com.
    VM connection via RDP WebAssembly in new browser tab.

    This is the evolution of the existing avd_session.py AVDSessionManager.
    """

    ENTRY_URL = "https://windows.microsoft.cloud"
    DASHBOARD_URL = "https://windows.cloud.microsoft/#/devices"
    CLIENT_ID = "451f2815-40fe-44bb-b8a6-3a2e55cf40c4"
    SCOPE = "https://www.wvd.microsoft.com/User.Access"
    FEED_API = "https://rdweb.wvd.microsoft.com/api/arm/feeddiscovery"

    async def get_vms(self) -> list[VmInfo]:
        """Query feed discovery API for provisioned desktops/apps."""

    async def connect(self, vm_id: str) -> VmConnection:
        """Open AVD web client session in a new tab.

        Navigates to: https://windows.cloud.microsoft/webclient/avd/<resourceId>
        """

    async def _fetch_vms(self, page) -> list[VmInfo]:
        """Internal: call feed discovery API via page.evaluate(fetch)."""

    async def _connect_vm(self, page, vm_id: str) -> VmConnection:
        """Internal: open AVD session tab, wait for WebSocket connect."""
```

### 4.3 `PortalSession` (Azure Portal backend)

```python
class PortalSession(AzureSession):
    """Session targeting the Azure portal (portal.azure.com).

    VM listing via ARM REST API (management.azure.com).
    VM connection via Bastion (new tab) or Serial Console (embedded blade).

    This is the NEW backend for infrastructure VM access.
    """

    ENTRY_URL = "https://portal.azure.com/"
    VM_LIST_URL = \
        "https://portal.azure.com/#browse/Microsoft.Compute/virtualMachines"
    CLIENT_ID = "c44b4083-3bb0-49c1-b47d-974e53cbdf3c"
    SCOPE = "https://management.core.windows.net//.default"
    ARM_API = "https://management.azure.com"

    async def get_vms(self) -> list[VmInfo]:
        """Query ARM REST API for VMs in the configured subscription.

        Extracts the Bearer token from the browser's MSAL localStorage
        cache, then calls:
        GET .../subscriptions/{id}/providers/Microsoft.Compute/virtualMachines
        ?api-version=2026-03-02
        """

    async def connect(
        self, vm_id: str, method: str = "bastion"
    ) -> VmConnection:
        """Open a remote session to the VM.

        Args:
            vm_id: ARM resource ID of the VM.
            method: "bastion" (default) or "serial_console".

        Bastion: clicks Connect → Bastion in the portal, captures
                 the new browser tab with the HTML5 client.
        Serial Console: clicks Connect → Serial Console, attaches
                 to the embedded iframe/blade within the portal tab.
        """

    async def _fetch_vms(self, page) -> list[VmInfo]:
        """Internal: extract token, call ARM API, parse VM resources."""

    async def _connect_vm_bastion(self, page, vm_id: str) -> VmConnection:
        """Internal: automate portal Bastion connect flow.

        1. Navigate to VM overview blade:
           https://portal.azure.com/#@<tenant>/resource/<vm_id>/overview
        2. Click "Connect" → "Bastion"
        3. Fill Bastion credentials (username/password from VM config)
        4. Click "Connect" button
        5. Wait for new tab to open (context.waitForEvent('page'))
        6. Return VmConnection wrapping the new tab
        """

    async def _connect_vm_serial(self, page, vm_id: str) -> VmConnection:
        """Internal: automate portal Serial Console flow.

        1. Navigate to VM overview
        2. Click "Connect" → "Serial Console"
        3. Wait for serial console blade/iframe to load
        4. Return VmConnection wrapping the console WebSocket
        """
```

### 4.4 Data types

```python
@dataclass
class Credentials:
    email: str
    password: str


@dataclass
class VmInfo:
    """Normalized VM descriptor across backends."""
    id: str               # Backend-specific resource ID
    name: str             # Display name
    backend: str          # "avd" or "portal"
    kind: str             # "desktop", "app", "virtualMachine"
    workspace: str        # AVD workspace name (empty for portal)
    location: str         # Azure region (portal only)
    power_state: str      # "running", "stopped", etc. (portal only)


@dataclass
class VmConnection:
    """Handle to an active VM remote session."""
    vm_id: str
    vm_name: str
    backend: str
    method: str           # "avd_rdp", "bastion_rdp", "bastion_ssh",
                          # "serial_console"
    page: "Page"          # Playwright Page for the connection tab
    connected_at: float   # time.monotonic() timestamp

    async def wait_for_disconnect(self) -> None:
        """Resolve when the connection tab closes."""

    async def close(self) -> None:
        """Close the connection tab."""

    @property
    def is_connected(self) -> bool:
        """True if the tab is still open and hasn't crashed."""


@dataclass
class SessionState:
    authenticated: bool
    on_vm_list: bool
    current_url: str
    vm_count: int
    backend: str
    mfa_pending: bool
    reauth_count: int
    last_reauth_time: float
    uptime_seconds: float
```

---

## 5. MFA Signal/Wait Pattern

### 5.1 Design

The library uses an `asyncio.Event` for MFA coordination:

```
Caller                          AzureSession
  │                                  │
  │  await session.start()           │
  │                                  ├─ navigate to portal
  │                                  ├─ fill email, password
  │                                  ├─ detect MFA prompt
  │                                  ├─ can't auto-resolve
  │                                  ├─ mfa_event.set()
  │  ◄── mfa_event is set ──────────┤
  │  prompt user for code            │  ... waiting ...
  │  session.provide_mfa_code("123456")
  │──────────────────────────────────►
  │                                  ├─ fill code, submit
  │                                  ├─ wait for redirect
  │                                  ├─ mfa_event.clear()
  │  ◄── returns True ──────────────┤
  │                                  │
```

### 5.2 MFA prompt types and handling

| Prompt Type | Auto-resolvable? | Handler |
|---|---|---|
| TOTP code input | Yes (if totp_secret set) | `_handle_totp()` |
| Push notification | Partial (poll for approval) | `_handle_push_pending()` |
| SMS code | No | Signal via `mfa_event` |
| Device/method selection | Partial (auto-select TOTP) | `_handle_device_selection()` |
| FIDO2 / Windows Hello | No | Signal via `mfa_event` |

The `"auto"` MFA method tries TOTP first, then push polling, then signals
the caller for manual intervention.

### 5.3 Usage example

```python
async def main():
    config = AzureConfig.from_env()
    session = PortalSession(config)

    # Start triggers login. If MFA is needed, mfa_event fires.
    started = await session.start()

    if session.mfa_pending:
        code = input("Enter MFA code: ")
        await session.provide_mfa_code(code)

    # Now authenticated — list VMs
    vms = await session.get_vms()
    for vm in vms:
        print(f"  {vm.name} ({vm.power_state}) — {vm.location}")

    # Connect to a VM
    conn = await session.connect(vms[0].id, method="bastion")
    print(f"Connected to {conn.vm_name} via {conn.method}")

    # Keep alive until Ctrl+C
    await session.run_loop()
```

---

## 6. Persistent Browser Context Strategy

### 6.1 Primary strategy: `launchPersistentContext`

```python
# In AzureSession._start_browser():
context = await p.chromium.launch_persistent_context(
    user_data_dir=config.user_data_dir,
    headless=not config.headed,
    user_agent=config.user_agent,
    viewport=config.viewport,
    args=config.chromium_args,
)
page = context.pages[0] if context.pages else await context.new_page()
```

**Advantages:**
- Zero manual state management — cookies, localStorage, IndexedDB, service
  workers all persist automatically
- MSAL.js token cache survives browser restarts
- `ESTSAUTHPERSISTENT` cookie preserved across launches
- Supports multi-day session reuse with periodic heartbeat navigation

**Constraints:**
- Single process lock — one instance per `userDataDir`
- Larger disk footprint (~50–200 MB)
- Mitigate corruption risk with periodic `userDataDir` backups

### 6.2 Fallback: `storageState` export/import

```python
# Export after successful login:
await context.storage_state(path=config.storage_state_path)

# Import on next launch:
context = await browser.new_context(
    storage_state=config.storage_state_path,
    user_agent=config.user_agent,
    viewport=config.viewport,
)
```

Used when `persistence_mode = "storage_state"` or when
`launchPersistentContext` fails (e.g., profile locked by another process).

### 6.3 Tab management

| Tab | Purpose | Lifecycle |
|---|---|---|
| Dashboard tab | VM list + portal navigation | Persistent, kept alive with heartbeat |
| Connection tab (N) | Active VM remote session (Bastion/AVD) | Created on demand, closed on disconnect |
| Serial Console | Debug console | Embedded in portal tab, not a separate tab |

New tabs are detected via `context.on('page')` or `context.waitForEvent('page')`.
Tab close (VM disconnect) is detected via `page.on('close')`.

---

## 7. Session Keep-Alive & Recovery

### 7.1 Heartbeat loop

```python
async def _heartbeat(self, page):
    """Called every config.heartbeat_interval seconds."""
    current_url = page.url
    if "login.microsoftonline.com" in current_url:
        # Session expired — trigger re-auth
        await self._reauthenticate()
    else:
        # Reload the VM list to exercise the access token
        await page.reload()
```

### 7.2 Re-authentication flow

1. Detect login redirect (`login.microsoftonline.com` in URL)
2. Check if stored auth state can restore the session
3. If not, perform full login again
4. MFA may be needed — use the same signal/wait pattern
5. Navigate back to VM list
6. Increment `reauth_count` and log event

### 7.3 Error recovery matrix

| Failure | Detection | Recovery |
|---|---|---|
| Access token expiry | 401 from ARM/feed API | MSAL.js silent refresh (automatic) |
| Refresh token expiry | Redirect to login page | Full re-authentication |
| Browser crash | `page.is_closed()` / CDP disconnect | Restart browser, load saved state |
| Profile corruption | `launchPersistentContext` exception | Fall back to `storageState` mode |
| Conditional Access block | Login error or redirect loop | Surface to caller; suggest headed mode |
| WebSocket disconnect | `page.on('close')` for connection tab | Reconnect VM (new `connect()` call) |

---

## 8. Security Considerations

### 8.1 Credential handling

- Credentials read from environment variables or `.env` file — never
  hardcoded
- `AzureConfig.password` is stored as a `str` in memory; the dataclass
  does NOT implement `__repr__` (password redacted)
- TOTP secret stored in env var `AZURE_TOTP_SECRET`, not in config files
- `auth-state.json` and `userDataDir` contain live session tokens — treat
  as secrets (`.gitignore` both)

### 8.2 Browser profile isolation

- Each `userDataDir` is a self-contained Chromium profile
- No shared state between instances unless explicitly configured
- Profile directory permissions: `0o700` on creation
- Backup `userDataDir` before long-running sessions to guard against
  corruption

### 8.3 Network security

- All traffic over HTTPS/WSS (TLS 1.2+)
- Bearer tokens extracted from browser localStorage for API calls
- Tokens never written to disk outside the browser profile
- No token logging at INFO level; DEBUG level redacts token values

### 8.4 MFA security

- TOTP secrets handled as sensitive strings
- `provide_mfa_code()` logs at DEBUG only (code value redacted)
- `mfa_event` pattern keeps MFA interaction out-of-band from the browser
  automation — the caller mediates all user interaction

---

## 9. Module Layout

```
workee/
├── azure_wrapper/
│   ├── __init__.py          # Public API: AzureConfig, PortalSession,
│   │                        # AVDSession, VmInfo, VmConnection, etc.
│   ├── config.py            # AzureConfig dataclass + from_env()
│   ├── session.py           # AzureSession abstract base class
│   ├── auth.py              # AuthManager — login flow, token extraction
│   ├── mfa.py               # MfaManager — detection, auto/manual handling
│   ├── monitor.py           # SessionMonitor — heartbeat, re-auth loop
│   ├── avd.py               # AVDSession implementation
│   ├── portal.py            # PortalSession implementation
│   └── types.py             # VmInfo, VmConnection, SessionState,
│                            # MfaResult, MfaPromptType, etc.
├── avd_session.py           # Legacy CLI (kept, wraps AVDSession)
├── avd_client.py            # Legacy sync client (kept)
├── avd_login.py             # Legacy login script (kept)
├── avd_mfa.py               # Legacy MFA module (kept, may be vendored)
└── list_vms.py              # Legacy thin wrapper (kept)
```

The new `azure_wrapper/` package is the canonical API. Existing scripts
are preserved for backward compatibility and can be gradually migrated.

---

## 10. Comparison with Existing Code

| Aspect | Current (`avd_session.py`) | New (`azure_wrapper/`) |
|---|---|---|
| Persistence | `storageState` only | `launchPersistentContext` (primary) + `storageState` (fallback) |
| Backend | AVD only | AVD + Azure Portal |
| MFA | Inline stdin blocking | Signal/wait (`asyncio.Event`) |
| VM listing | Feed discovery API | Feed discovery (AVD) + ARM API (Portal) |
| VM connection | Click by name on dashboard | Programmatic `connect(vm_id)` with backend dispatch |
| Config | Manual `os.getenv()` calls | `AzureConfig` dataclass with `from_env()` |
| Architecture | Monolithic script | Package with separated concerns |

---

## 11. Usage Examples

### 11.1 CLI: list portal VMs

```bash
python -m azure_wrapper --backend portal --list
```

### 11.2 CLI: connect to a portal VM via Bastion

```bash
python -m azure_wrapper --backend portal --connect /subscriptions/.../vm-name
```

### 11.3 Programmatic: headless long-running dashboard

```python
import asyncio
from azure_wrapper import AzureConfig, PortalSession

async def main():
    config = AzureConfig.from_env()
    config.backend = "portal"

    async with PortalSession(config) as session:
        vms = await session.get_vms()
        running = [v for v in vms if v.power_state == "running"]

        conn = await session.connect(running[0].id, method="bastion")
        print(f"Opened Bastion to {conn.vm_name}")

        # Keep session alive, monitor for disconnect
        await conn.wait_for_disconnect()
        print("VM session ended")

asyncio.run(main())
```

### 11.4 Programmatic: MFA with custom prompt

```python
async def main():
    config = AzureConfig.from_env()
    session = PortalSession(config)

    # Start in background — login will trigger MFA if needed
    start_task = asyncio.create_task(session.start())

    # Wait for MFA or completion
    done, pending = await asyncio.wait(
        [start_task, asyncio.create_task(session.mfa_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if session.mfa_pending:
        code = await my_custom_prompt("Enter MFA code:")
        await session.provide_mfa_code(code)
        await start_task  # wait for login to finish

    vms = await session.get_vms()
    # ...
```

---

## 12. Implementation Phases

### Phase 1: Core abstractions
- `AzureConfig`, `SessionState`, `VmInfo`, `VmConnection` types
- `AzureSession` ABC with lifecycle (`start`, `stop`, `run_loop`)
- `AuthManager` extracted from `avd_login.py`
- `MfaManager` with signal/wait pattern (extend `avd_mfa.py`)

### Phase 2: AVD backend
- `AVDSession` implements `get_vms`, `connect` using existing logic
- Migrate `avd_session.py` CLI to wrap `AVDSession`

### Phase 3: Portal backend
- `PortalSession` implements `get_vms` (ARM API) and `connect` (Bastion)
- Test with real Azure portal session

### Phase 4: Hardening
- `SessionMonitor` heartbeat and re-auth loop
- Error recovery matrix
- Profile corruption fallback
- Logging and observability
