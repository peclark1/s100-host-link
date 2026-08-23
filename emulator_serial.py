#!/usr/bin/env python3
"""Discovery helpers for the z80pack IMSAI target emulator serial endpoint."""
from __future__ import annotations

import os


def emulator_tty_path() -> str:
    """Return the running targetsim Serial I/O USB PTY path, or an empty string."""
    configured = os.environ.get("TARGET_SERIALIO_USB_TTY", "").strip()
    if configured:
        candidate = configured
    elif os.name == "posix" and hasattr(os, "getuid"):
        candidate = f"/tmp/targets100sim-usb-{os.getuid()}"
    else:
        return ""

    return candidate if os.path.exists(candidate) else ""
