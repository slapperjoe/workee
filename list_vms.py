#!/usr/bin/env python3
"""
Thin entry-point wrapper for AVD VM listing.

This script delegates all logic to avd_client.py — its only job is to
load environment variables and invoke the client.

Usage:
    export AVD_EMAIL='user@contoso.com'
    export AVD_PASSWORD='your-password'
    python3 list_vms.py

    # With saved session (after MFA solved interactively):
    export AVD_STORAGE_STATE=~/.avd_session.json
    python3 list_vms.py
"""

import sys

from avd_client import main

if __name__ == "__main__":
    main()
