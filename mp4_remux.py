#!/usr/bin/env python3
"""
RoadShield — mp4_remux.py
=========================
MediaMTX records fragmented MP4 (recordFormat: fmp4): an empty `moov` and
~one `moof` fragment per second, so a player has to walk the whole file to
learn its duration or seek — very heavy on PCs, and QuickTime/macOS
handles it badly. H.265 cameras are also tagged `hev1`, which Apple players
refuse outright (they only accept `hvc1`).

remux_for_upload() rewrites a recording into a regular MP4 with its full
index at the front (+faststart) and H.265 tagged hvc1 — stream copy only
(`-c copy`): no re-encode, identical quality, ~same size, a few seconds per
file. The local recording is left untouched; a temp copy is uploaded.
"""

import glob
import os
import subprocess
import tempfile

TMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".upload-tmp")
REMUX_TIMEOUT = 600
# Low CPU/IO priority so remuxing never competes with live streaming/recording.
_LOW_PRIORITY = ["nice", "-n", "10", "ionice", "-c", "3"]


def cleanup_stale_temp_files():
    """Remove temp copies left behind by a crash/kill mid-upload."""
    for path in glob.glob(os.path.join(TMP_DIR, "*.mp4")):
        try:
            os.remove(path)
        except OSError:
            pass


def _video_codec(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    return out.splitlines()[0] if out else ""


def remux_for_upload(path, log=None):
    """
    Returns (upload_path, is_temp). On any failure falls back to
    (path, False) so the original is uploaded as before.
    """
    os.makedirs(TMP_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".mp4", dir=TMP_DIR)
    os.close(fd)
    try:
        cmd = _LOW_PRIORITY + [
            "ffmpeg", "-v", "error", "-y", "-i", path,
            "-map", "0:v", "-map", "0:a?", "-c", "copy",
        ]
        if _video_codec(path) == "hevc":
            cmd += ["-tag:v", "hvc1"]
        cmd += ["-movflags", "+faststart", "-f", "mp4", tmp_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=REMUX_TIMEOUT)
        if proc.returncode == 0 and os.path.getsize(tmp_path) > 0:
            return tmp_path, True
        if log:
            log.warning(f"[remux] {path} failed (rc={proc.returncode}): {proc.stderr[:200]} — uploading original")
    except Exception as e:
        if log:
            log.warning(f"[remux] {path} failed: {e} — uploading original")
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return path, False
