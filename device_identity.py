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
# Hostname the id was created on. A disk image cloned onto a new unit keeps
# ~/.roadshield, so the new unit would report as the old one (same id, same
# terminal key); once the clone is given its own hostname, that mismatch
# tells us to start a fresh identity.
_HOST_FILE = os.path.join(_ID_DIR, "hostname")


def _detect_clone() -> None:
    host = socket.gethostname()
    try:
        with open(_HOST_FILE) as f:
            recorded = f.read().strip()
    except OSError:
        recorded = None
    if recorded is None:
        if os.path.exists(_ID_FILE):
            try:
                with open(_HOST_FILE, "w") as f:
                    f.write(host)
            except OSError:
                pass
        return
    if recorded and recorded != host:
        backup = f"{_ID_DIR}.{recorded}-clone-backup"
        try:
            os.rename(_ID_DIR, backup)
        except OSError:
            return


def get_device_id() -> str:
    """Return this device's persistent id, generating it on first call."""
    _detect_clone()
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
        with open(_HOST_FILE, "w") as f:
            f.write(socket.gethostname())
    except Exception:
        # Filesystem not writable — fall back to a per-process id derived
        # from the hostname so at least it's stable across restarts on
        # the same box, even though it won't survive a reimage.
        return f"srv-{socket.gethostname()}"

    return new_id


if __name__ == "__main__":
    print(get_device_id())
