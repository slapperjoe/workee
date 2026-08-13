# Azure Virtual Desktop Chromium wrapper

This wrapper keeps the Azure Virtual Desktop dashboard open in Chromium and
opens a VM connection in a separate tab when requested. Microsoft Entra ID
login and MFA remain in the browser flow; do not attempt to bypass MFA.

## Setup

Run these commands from this directory:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp config.example.env .env
```

Edit `.env` and set `AVD_EMAIL` and `AVD_PASSWORD`. Keep `.env` and the
generated `auth-state.json` private. For MFA, use `AVD_MFA_METHOD=manual` to
enter the code at the terminal, or set `AVD_MFA_METHOD=totp` and provide
`AVD_TOTP_SECRET` when your organisation permits TOTP. Authenticator push,
QR-code enrolment, FIDO2, and other interactive challenges require normal
browser interaction and cannot be automated by this wrapper.

## VM Launch Dashboard

The dashboard provides a web UI for browsing and connecting to Azure VMs:

```sh
.venv/bin/python dashboard_server.py
# Dashboard: http://localhost:8080
```

Options:
```sh
.venv/bin/python dashboard_server.py --headed    # show browser during MFA
.venv/bin/python dashboard_server.py --port 9090  # custom port
.venv/bin/python dashboard_server.py --backend portal  # Azure Portal VMs
```

The dashboard shows:
- VM tiles with power state, workspace, and location
- One-click Connect for running VMs
- Start & Connect for stopped VMs (Portal backend)
- Active connections panel
- MFA code entry when required

Stored VM favorites can be managed in `dashboard/config.json`.

## Run

Start the persistent dashboard session:

```sh
.venv/bin/python avd_session.py
```

Show the browser window while debugging login or MFA:

```sh
.venv/bin/python avd_session.py --headed
```

List available machines and exit:

```sh
.venv/bin/python avd_session.py --list
```

Open a machine by name and keep the dashboard/session manager alive:

```sh
.venv/bin/python avd_session.py --vm "Machine Name"
```

Open a machine and exit the wrapper immediately after the connection tab is
opened:

```sh
.venv/bin/python avd_session.py --vm "Machine Name" --no-keep
```

Interactive mode accepts `list`, `connect <name>`, `status`, `reauth`, and
`quit` commands:

```sh
.venv/bin/python avd_session.py --interactive
```

The first successful login saves Playwright storage state to
`AVD_STORAGE_STATE_PATH` (default `./auth-state.json`) so subsequent launches
can reuse the session until Microsoft expires it. The session manager detects
expiry and retries login; if MFA requires a human, follow the prompt or use
`--headed`.

## Login-only mode

```sh
.venv/bin/python avd_login.py
```

## Tests

```sh
.venv/bin/pytest -q
```

`test_selectors.py` is a manual live selector probe and can be run directly:

```sh
.venv/bin/python test_selectors.py
```

## Screen Sharing (Host → VM → Teams)

Share the host desktop into Microsoft Teams running inside the VM via NDI
network streaming. Zero hardware, low latency.

```sh
# Host setup (one-time):
./screen_sharing/setup_host_ndi.sh

# Guest setup (one-time, run inside Windows VM):
# .\screen_sharing\setup_guest_ndi.ps1

# Daily use:
obs --profile NDI_ScreenShare --collection NDI_ScreenShare
python screen_sharing/ndi_control.py status
```

Full documentation: [screen_sharing/README.md](screen_sharing/README.md)
