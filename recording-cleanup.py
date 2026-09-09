#!/usr/bin/env python3
"""
RoadShield — recording-cleanup.py
===================================
Monitors the recordings directory and enforces a maximum size limit per
stream tier (see TIERS below): "main" (full-res) directories and "sub"
(low-res, name ends in "_sub") directories are capped independently. When
a tier's total size exceeds its max_gb, that tier's oldest recording files
are deleted until it drops below its target_gb (safety margin).

Runs as a background daemon alongside MediaMTX and camera-control.py.

Usage:
  python3 recording-cleanup.py
"""

import os
import time
import logging
import signal
import sys

# ─── Configuration ──────────────────────────────────────────────────────────
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
CHECK_INTERVAL = 60    # Seconds between size checks

# Main (full-res) and sub (low-res) streams are capped independently — a
# top-level recordings/ subdirectory belongs to the "sub" tier iff its name
# ends in "_sub" (e.g. recordings/cam1_sub/...), everything else is "main".
# Main is the local high-quality archive, kept small; sub is what normally
# gets synced (already small), so its cap is just a safety net against an
# offline device filling the disk — not meant to be hit in normal use.
TIERS = {
    "main": {"max_gb": 100, "target_gb": 90},
    "sub":  {"max_gb": 50,  "target_gb": 45},
}


def tier_of(top_dir: str) -> str:
    """Classify a top-level recordings/ subdirectory as 'main' or 'sub'."""
    return "sub" if top_dir.endswith("_sub") else "main"

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [rec-cleanup] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Graceful shutdown ──────────────────────────────────────────────────────
_shutdown = False

def signal_handler(sig, frame):
    global _shutdown
    log.info("Shutdown signal received.")
    _shutdown = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def get_dir_size_bytes(path: str, tier: str = None) -> int:
    """
    Calculate total size of all files in a directory tree (bytes).
    If `tier` is given, only counts files under top-level subdirectories
    belonging to that tier ('main' or 'sub').
    """
    total = 0
    try:
        for entry in os.scandir(path):
            if not entry.is_dir():
                continue
            if tier is not None and tier_of(entry.name) != tier:
                continue
            for dirpath, _dirnames, filenames in os.walk(entry.path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
    except OSError:
        pass
    return total


def bytes_to_gb(b: int) -> float:
    """Convert bytes to gigabytes."""
    return b / (1024 ** 3)


def gb_to_bytes(gb: float) -> int:
    """Convert gigabytes to bytes."""
    return int(gb * (1024 ** 3))


def get_all_recording_files(path: str, tier: str = None) -> list:
    """
    Get all recording files sorted by modification time (oldest first).
    If `tier` is given, only includes files under top-level subdirectories
    belonging to that tier ('main' or 'sub').
    Returns list of (filepath, size_bytes, mtime) tuples.
    """
    files = []
    try:
        for entry in os.scandir(path):
            if not entry.is_dir():
                continue
            if tier is not None and tier_of(entry.name) != tier:
                continue
            for dirpath, _dirnames, filenames in os.walk(entry.path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        stat = os.stat(fp)
                        files.append((fp, stat.st_size, stat.st_mtime))
                    except OSError:
                        pass
    except OSError:
        pass

    # Sort by modification time — oldest first
    files.sort(key=lambda x: x[2])
    return files


def cleanup_recordings():
    """
    Check each tier's (main/sub) recordings size independently and delete
    that tier's oldest files if it's over its own limit. A full sub-stream
    directory never counts against — or gets trimmed by — the main cap, and
    vice versa.
    """
    if not os.path.isdir(RECORDINGS_DIR):
        return

    for tier, limits in TIERS.items():
        _cleanup_tier(tier, limits["max_gb"], limits["target_gb"])


def _cleanup_tier(tier: str, max_gb: float, target_gb: float):
    total_bytes = get_dir_size_bytes(RECORDINGS_DIR, tier=tier)
    total_gb = bytes_to_gb(total_bytes)
    max_bytes = gb_to_bytes(max_gb)
    target_bytes = gb_to_bytes(target_gb)

    if total_bytes <= max_bytes:
        log.debug(f"[{tier}] Recordings size: {total_gb:.2f} GB — within limit ({max_gb} GB)")
        return

    log.warning(
        f"[{tier}] Recordings size {total_gb:.2f} GB exceeds limit of {max_gb} GB. "
        f"Cleaning up to {target_gb} GB..."
    )

    files = get_all_recording_files(RECORDINGS_DIR, tier=tier)
    deleted_count = 0
    deleted_bytes = 0

    for filepath, file_size, mtime in files:
        if total_bytes <= target_bytes:
            break

        try:
            os.remove(filepath)
            total_bytes -= file_size
            deleted_count += 1
            deleted_bytes += file_size
            log.info(f"[{tier}] Deleted: {filepath} ({bytes_to_gb(file_size):.3f} GB)")
        except OSError as e:
            log.error(f"[{tier}] Failed to delete {filepath}: {e}")

    # Clean up empty directories
    _remove_empty_dirs(RECORDINGS_DIR)

    log.info(
        f"[{tier}] Cleanup complete: deleted {deleted_count} files "
        f"({bytes_to_gb(deleted_bytes):.2f} GB freed). "
        f"Current size: {bytes_to_gb(total_bytes):.2f} GB"
    )


def _remove_empty_dirs(path: str):
    """Remove empty subdirectories (but not the root recordings dir)."""
    try:
        for dirpath, dirnames, filenames in os.walk(path, topdown=False):
            if dirpath == path:
                continue
            if not os.listdir(dirpath):
                try:
                    os.rmdir(dirpath)
                    log.debug(f"Removed empty directory: {dirpath}")
                except OSError:
                    pass
    except OSError:
        pass


def main():
    log.info("=" * 60)
    log.info("RoadShield Recording Cleanup Service")
    log.info(f"  Recordings : {RECORDINGS_DIR}")
    for tier, limits in TIERS.items():
        log.info(f"  [{tier}] max: {limits['max_gb']} GB, target: {limits['target_gb']} GB")
    log.info(f"  Check every: {CHECK_INTERVAL}s")
    log.info("=" * 60)

    # Ensure recordings directory exists
    os.makedirs(RECORDINGS_DIR, exist_ok=True)

    while not _shutdown:
        try:
            cleanup_recordings()
        except Exception as e:
            log.error(f"Unexpected error during cleanup: {e}")

        # Sleep in small intervals to allow graceful shutdown
        for _ in range(CHECK_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Recording cleanup service stopped.")


if __name__ == "__main__":
    main()
