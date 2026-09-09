#!/usr/bin/env python3
"""
RoadShield — device_identity.py
================================
Each physical device gets a unique id generated once, locally, on first
run — never typed by a human, never duplicated by cloning this codebase
onto another device. This is the fix for cameras breaking whenever the
same code is copied to a new Rock/Jetson unit: identity used to be a
hardcoded string (JETSON_NAME) that had to be manually changed per
device, and forgetting to do so caused two devices to collide under the
same id.

Usage:
    from device_identity import get_device_id
    SERVER_ID = get_device_id()
"""

import os
import socket
import uuid

_ID_DIR = os.path.expanduser("~/.roadshield")
_ID_FILE = os.path.join(_ID_DIR, "device_id")


def get_device_id() -> str:
    """Return this device's persistent id, generating it on first call."""
    try:
        if os.path.exists(_ID_FILE):
            with open(_ID_FILE) as f:
                existing = f.read().strip()
                if existing:
                    return existing
    except Exception:
        pass

    new_id = f"srv-{uuid.uuid4().hex[:8]}"

    try:
        os.makedirs(_ID_DIR, exist_ok=True)
        with open(_ID_FILE, "w") as f:
            f.write(new_id)
    except Exception:
        # Filesystem not writable — fall back to a per-process id derived
        # from the hostname so at least it's stable across restarts on
        # the same box, even though it won't survive a reimage.
        return f"srv-{socket.gethostname()}"

    return new_id


if __name__ == "__main__":
    print(get_device_id())
