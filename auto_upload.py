#!/usr/bin/env python3
"""
RoadShield — auto_upload.py
===========================
Background queue that uploads every not-yet-uploaded recording to the
dashboard on its own — no "Start Sync" from the dashboard needed — while
keeping the uplink free for live viewing:

  * Order: all "sub" (low-res, dir ends in "_sub") recordings first, then
    "main"; within each tier newest → oldest by the date/time in the
    recording's path (camX/YYYY-MM-DD/HH-MM-SS.mp4). The queue is rebuilt
    after every file, so a freshly finished sub recording jumps ahead of
    older main ones.
  * File names/paths are uploaded unchanged (same relative_path as the
    manual sync), so the server-side layout stays date-based. Content is
    remuxed to a regular MP4 first (lossless, see mp4_remux.py).
  * Pauses between chunks the moment anyone is watching a live stream
    (HLS / WebRTC viewer, or an RTSP reader from outside this device), and
    resumes the same file automatically once nobody has been watching for
    RESUME_GRACE seconds.
  * Steps aside completely while a manual dashboard sync is running.
  * The recording currently being written is skipped until it is finished.
  * Local files are NOT deleted — recording-cleanup.py still handles disk
    space. Uploaded files are remembered in ~/.roadshield/uploaded_recordings.json
    (and double-checked against the server's verify endpoint), so nothing
    is uploaded twice, even across restarts.

Extra pause conditions (e.g. "stop uploading once GPS says we've arrived at
the site") plug in via AutoUploader.add_block_check(fn): fn() returns a
reason string to pause, or None to allow uploading.
"""

import json
import os
import threading
import time
from datetime import datetime

import requests

from mp4_remux import cleanup_stale_temp_files, remux_for_upload

CHUNK_SIZE = 10 * 1024 * 1024      # must match the manual sync's chunking
MEDIAMTX_API = "http://127.0.0.1:9997"
CHECK_INTERVAL = 2                 # seconds between live-viewer checks while blocked
RESUME_GRACE = 15                  # seconds with no viewers before uploading again
IDLE_RESCAN = 60                   # seconds between scans when nothing is pending
FILE_SETTLE_SECONDS = 120          # unmodified this long → recording is finished
RETRY_AFTER_FAILURE = 600          # seconds before retrying a file that failed
NETWORK_BACKOFF = 60               # pause after a failure before trying the next file
STATE_FILE = os.path.expanduser("~/.roadshield/uploaded_recordings.json")

# MediaMTX reader types that mean a person is watching. rtspSession is only
# counted when it comes from outside this device — local consumers (AI
# detection, transcoder) read RTSP over loopback and aren't viewers.
VIEWER_READER_TYPES = {"hlsMuxer", "webRTCSession"}
REMOTE_CHECK_READER_TYPES = {"rtspSession": "rtspsessions", "rtspsSession": "rtspssessions"}


class _Abort(Exception):
    """Current file must be dropped (manual sync took over) — retried later from scratch."""


class AutoUploader(threading.Thread):
    def __init__(self, recordings_dir, laravel_url, token, server_id, log,
                 is_manual_sync_running=lambda: False):
        super().__init__(daemon=True, name="auto-upload")
        self.recordings_dir = recordings_dir
        base = laravel_url.rstrip("/") + "/api/surveillance/recordings"
        self.chunk_url = base + "/upload-chunk"
        self.verify_url = base + "/verify"
        self.token = token
        self.server_id = server_id
        self.log = log
        self.is_manual_sync_running = is_manual_sync_running
        self._block_checks = [self._manual_sync_check, self._live_viewer_check]
        self._uploaded = self._load_state()
        self._failed_until = {}  # rel_path -> retry-after timestamp
        self._last_block_reason = None
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"

    # ── Pause conditions ───────────────────────────────────────────────────
    def add_block_check(self, fn):
        """fn() -> reason string to pause uploading, or None. E.g. GPS geofence."""
        self._block_checks.append(fn)

    def _manual_sync_check(self):
        return "manual sync running" if self.is_manual_sync_running() else None

    def _live_viewer_check(self):
        try:
            paths = requests.get(f"{MEDIAMTX_API}/v3/paths/list", timeout=2).json().get("items", [])
        except Exception:
            return None  # MediaMTX down → nobody can be watching live
        watching = []
        for p in paths:
            for reader in p.get("readers", []):
                rtype = reader.get("type")
                if rtype in VIEWER_READER_TYPES or (
                        rtype in REMOTE_CHECK_READER_TYPES and self._is_remote(rtype, reader.get("id"))):
                    watching.append(f"{p.get('name')}({rtype})")
        return f"live viewers: {', '.join(watching)}" if watching else None

    def _is_remote(self, rtype, session_id):
        try:
            s = requests.get(f"{MEDIAMTX_API}/v3/{REMOTE_CHECK_READER_TYPES[rtype]}/get/{session_id}",
                             timeout=2).json()
            host = s.get("remoteAddr", "").rsplit(":", 1)[0].strip("[]")
            return not (host.startswith("127.") or host == "::1")
        except Exception:
            return True  # can't tell → assume a real viewer, protect the live stream

    def _block_reason(self):
        for check in self._block_checks:
            try:
                reason = check()
            except Exception as e:
                reason = None
                self.log.warning(f"[auto-upload] block check {check} failed: {e}")
            if reason:
                return reason
        return None

    def _wait_until_clear(self, abort_on_manual=False):
        """Block while any pause condition holds; then require RESUME_GRACE
        seconds of continuous all-clear. With abort_on_manual (mid-file),
        raises _Abort if a manual sync took over — its upload must not
        interleave with ours."""
        clear_since = None
        while True:
            reason = self._block_reason()
            if reason:
                clear_since = None
                if reason != self._last_block_reason:
                    self.log.info(f"[auto-upload] paused — {reason}")
                    self._last_block_reason = reason
                if abort_on_manual and reason == "manual sync running":
                    raise _Abort(reason)
            else:
                if self._last_block_reason is None:
                    return
                clear_since = clear_since or time.time()
                if time.time() - clear_since >= RESUME_GRACE:
                    self.log.info("[auto-upload] resumed — no live viewers")
                    self._last_block_reason = None
                    return
            time.sleep(CHECK_INTERVAL)

    # ── Queue ──────────────────────────────────────────────────────────────
    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._uploaded, f)
        os.replace(tmp, STATE_FILE)

    @staticmethod
    def _recorded_at(rel_path, path):
        parts = rel_path.split("/")
        try:
            return datetime.strptime(f"{parts[-2]} {os.path.splitext(parts[-1])[0][:8]}",
                                     "%Y-%m-%d %H-%M-%S").timestamp()
        except (ValueError, IndexError):
            return os.path.getmtime(path)

    def _pending(self):
        now = time.time()
        pending, existing = [], set()
        for root, _dirs, files in os.walk(self.recordings_dir):
            for name in files:
                if not name.endswith(".mp4") or ".sync1080p." in name:
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, self.recordings_dir).replace(os.sep, "/")
                existing.add(rel)
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    continue
                if (st.st_size == 0 or now - st.st_mtime < FILE_SETTLE_SECONDS
                        or self._uploaded.get(rel) == st.st_size
                        or self._failed_until.get(rel, 0) > now):
                    continue
                tier = 0 if rel.split("/")[0].endswith("_sub") else 1
                pending.append((tier, -self._recorded_at(rel, path), path, rel, st.st_size))
        # Forget files recording-cleanup.py has deleted.
        stale = [k for k in self._uploaded if k not in existing]
        if stale:
            for k in stale:
                del self._uploaded[k]
            self._save_state()
        pending.sort()
        return pending

    # ── Upload ─────────────────────────────────────────────────────────────
    def _verify(self, rel, size):
        """Returns (verified, server_size)."""
        r = self._session.post(self.verify_url, timeout=30, json={
            "jetson_name": self.server_id, "relative_path": rel, "expected_size": size})
        r.raise_for_status()
        data = r.json()
        return data.get("verified") is True, data.get("server_size")

    def _upload(self, path, rel, _local_size):
        # Upload a regular-MP4 copy (index at front, hvc1) — see mp4_remux.py.
        upload_path, is_temp = remux_for_upload(path, self.log)
        try:
            return self._upload_file(upload_path, rel, os.path.getsize(upload_path))
        finally:
            if is_temp:
                try:
                    os.remove(upload_path)
                except OSError:
                    pass

    def _upload_file(self, path, rel, size):
        verified, server_size = self._verify(rel, size)
        if verified:
            return "already on server"
        # A different-size copy on the server (e.g. an interrupted earlier
        # upload) must be overwritten, or chunk 0 just reports "skipped".
        overwrite = "1" if server_size is not None else "0"
        total_chunks = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
        t0 = time.time()
        with open(path, "rb") as f:
            for idx in range(total_chunks):
                self._wait_until_clear(abort_on_manual=True)  # pause here while someone watches live
                data = f.read(CHUNK_SIZE)
                for attempt in range(3):
                    try:
                        r = self._session.post(self.chunk_url, timeout=120, files={
                            "file": (f"chunk_{idx}", data, "application/octet-stream")}, data={
                            "jetson_name": self.server_id, "relative_path": rel,
                            "chunk_index": str(idx), "total_chunks": str(total_chunks),
                            "overwrite": overwrite})
                        body = r.json() if r.status_code == 200 else {}
                        if body.get("success"):
                            break
                        err = body.get("error") or f"HTTP {r.status_code}: {r.text[:200]}"
                    except Exception as e:
                        err = str(e)
                    time.sleep(2)
                else:
                    raise RuntimeError(f"chunk {idx}/{total_chunks} failed: {err}")
                if idx == 0 and body.get("status") == "skipped":
                    break
        if not self._verify(rel, size)[0]:
            raise RuntimeError("server verification failed after upload (missing or size mismatch)")
        mbps = size * 8 / 1e6 / max(time.time() - t0, 0.001)
        return f"uploaded {size / 1e6:.1f}MB at {mbps:.1f}Mbps"

    def run(self):
        cleanup_stale_temp_files()
        self.log.info(f"[auto-upload] started — {len(self._uploaded)} files already uploaded")
        while True:
            try:
                self._wait_until_clear()
                pending = self._pending()
                if not pending:
                    time.sleep(IDLE_RESCAN)
                    continue
                _tier, _ts, path, rel, size = pending[0]
                self.log.info(f"[auto-upload] {rel} ({size / 1e6:.1f}MB) — {len(pending)} in queue")
                try:
                    result = self._upload(path, rel, size)
                except _Abort:
                    continue  # manual sync took over; _wait_until_clear() blocks until it ends
                except FileNotFoundError:
                    continue  # deleted by recording-cleanup.py meanwhile
                except Exception as e:
                    self.log.warning(f"[auto-upload] {rel} failed: {e} — retrying in {RETRY_AFTER_FAILURE}s")
                    self._failed_until[rel] = time.time() + RETRY_AFTER_FAILURE
                    time.sleep(NETWORK_BACKOFF)
                    continue
                self._uploaded[rel] = size
                self._failed_until.pop(rel, None)
                self._save_state()
                self.log.info(f"[auto-upload] {rel}: {result}")
            except Exception as e:  # never let the thread die
                self.log.error(f"[auto-upload] unexpected error: {e}")
                time.sleep(NETWORK_BACKOFF)
