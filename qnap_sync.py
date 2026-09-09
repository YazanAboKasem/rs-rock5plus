#!/usr/bin/env python3
"""
RoadShield — qnap_sync.py
============================
QNAP NAS recording sync module. Uploads local recordings to a remote QNAP NAS
via SMB, SFTP, or WebDAV. Triggered via WebSocket events from the Laravel dashboard.

Supports:
  - Full sync (all recordings) or filtered (by date / camera)
  - Progress reporting via WebSocket
  - Pause / Resume / Cancel
  - Auto-detection of upload protocol
  - Optional deletion of local files after upload

Events handled:
  sync.start  → validate connection & begin upload
  sync.pause  → pause active upload
  sync.resume → resume paused upload
  sync.cancel → cancel active upload
"""

import os
import time
import threading
import logging
import subprocess
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

log = logging.getLogger("qnap-sync")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(SCRIPT_DIR, "recordings")

# Upload progress reporting interval (seconds)
PROGRESS_REPORT_INTERVAL = 5


class QNAPSyncManager:
    """
    Manages recording uploads to a QNAP NAS.
    Runs in a background thread with pause/resume/cancel support.
    """

    def __init__(self, ws_client=None):
        self.ws_client = ws_client
        self._active_sync: Optional[SyncJob] = None
        self._lock = threading.Lock()

    def start_sync(self, data: dict) -> dict:
        """
        Handle sync.start event. Validates QNAP connection and starts upload.

        Returns ack dict with status.
        """
        request_id = data.get("request_id", "unknown")
        qnap_config = data.get("qnap", {})
        options = data.get("options", {})

        with self._lock:
            if self._active_sync and self._active_sync.is_alive():
                return {
                    "request_id": request_id,
                    "status": "error",
                    "error": "A sync is already in progress. Cancel it first.",
                }

        # Validate QNAP config
        required_fields = ["host", "username", "password", "remote_path"]
        missing = [f for f in required_fields if not qnap_config.get(f)]
        if missing:
            return {
                "request_id": request_id,
                "status": "error",
                "error": f"Missing QNAP config fields: {', '.join(missing)}",
            }

        # Collect files to upload
        files = self._collect_files(options)
        if not files:
            return {
                "request_id": request_id,
                "status": "error",
                "error": "No recording files found matching the criteria.",
            }

        total_size = sum(f["size"] for f in files)
        total_size_gb = total_size / (1024 ** 3)

        # Detect upload method
        upload_method = self._detect_upload_method(qnap_config)
        if not upload_method:
            return {
                "request_id": request_id,
                "status": "error",
                "error": "Cannot connect to QNAP. Tried SMB, SFTP, and WebDAV.",
            }

        log.info(
            f"[sync] Starting upload: {len(files)} files, "
            f"{total_size_gb:.2f} GB, method={upload_method}"
        )

        # Estimate time (assume ~10 Mbps average)
        estimated_minutes = max(1, int((total_size / (10 * 1024 * 1024)) / 60))

        # Start sync job in background thread
        job = SyncJob(
            request_id=request_id,
            files=files,
            qnap_config=qnap_config,
            options=options,
            upload_method=upload_method,
            ws_client=self.ws_client,
        )

        with self._lock:
            self._active_sync = job

        job.start()

        return {
            "request_id": request_id,
            "status": "started",
            "total_files": len(files),
            "total_size_gb": round(total_size_gb, 2),
            "estimated_time_minutes": estimated_minutes,
            "upload_method": upload_method,
        }

    def pause_sync(self, data: dict) -> dict:
        """Handle sync.pause event."""
        request_id = data.get("request_id", "unknown")

        with self._lock:
            if not self._active_sync or not self._active_sync.is_alive():
                return {"request_id": request_id, "status": "error", "error": "No active sync"}

            self._active_sync.pause()
            return {
                "request_id": request_id,
                "status": "paused",
                "files_uploaded": self._active_sync.files_uploaded,
            }

    def resume_sync(self, data: dict) -> dict:
        """Handle sync.resume event."""
        request_id = data.get("request_id", "unknown")

        with self._lock:
            if not self._active_sync or not self._active_sync.is_alive():
                return {"request_id": request_id, "status": "error", "error": "No active sync"}

            self._active_sync.resume()
            return {
                "request_id": request_id,
                "status": "resumed",
                "files_uploaded": self._active_sync.files_uploaded,
            }

    def cancel_sync(self, data: dict) -> dict:
        """Handle sync.cancel event."""
        request_id = data.get("request_id", "unknown")

        with self._lock:
            if not self._active_sync or not self._active_sync.is_alive():
                return {"request_id": request_id, "status": "error", "error": "No active sync"}

            self._active_sync.cancel()
            return {
                "request_id": request_id,
                "status": "cancelled",
                "files_uploaded": self._active_sync.files_uploaded,
            }

    # ─── File Collection ─────────────────────────────────────────────────

    def _collect_files(self, options: dict) -> list:
        """
        Collect recording files based on sync options.

        Returns list of dicts: [{"path": str, "relative": str, "size": int}, ...]
        """
        if not os.path.isdir(RECORDINGS_DIR):
            return []

        scope = options.get("scope", "all")
        cameras = options.get("cameras", [])
        days = options.get("days")

        files = []
        now = datetime.now()

        for dirpath, _dirnames, filenames in os.walk(RECORDINGS_DIR):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                relative = os.path.relpath(filepath, RECORDINGS_DIR)

                try:
                    stat = os.stat(filepath)
                except OSError:
                    continue

                # Apply filters
                if scope == "today":
                    file_date = datetime.fromtimestamp(stat.st_mtime)
                    if file_date.date() != now.date():
                        continue

                elif scope == "last_n_days" and days:
                    file_date = datetime.fromtimestamp(stat.st_mtime)
                    cutoff = now - timedelta(days=int(days))
                    if file_date < cutoff:
                        continue

                elif scope == "cameras" and cameras:
                    # Check if file belongs to one of the selected cameras
                    # Assumes structure: recordings/cam1/date/file.mp4
                    parts = relative.split(os.sep)
                    if parts and parts[0] not in cameras:
                        continue

                files.append({
                    "path": filepath,
                    "relative": relative,
                    "size": stat.st_size,
                })

        # Sort by modification time (oldest first)
        files.sort(key=lambda f: os.path.getmtime(f["path"]))
        return files

    # ─── Upload Method Detection ─────────────────────────────────────────

    def _detect_upload_method(self, qnap_config: dict) -> Optional[str]:
        """
        Try to detect which upload method works with the QNAP NAS.
        Returns: "smbclient", "scp", "rclone", or None
        """
        host = qnap_config["host"]
        username = qnap_config["username"]
        password = qnap_config["password"]
        port = qnap_config.get("port", 445)

        # 1. Try smbclient
        if shutil.which("smbclient"):
            try:
                result = subprocess.run(
                    [
                        "smbclient", f"//{host}/Recordings",
                        "-U", f"{username}%{password}",
                        "-p", str(port),
                        "-c", "ls",
                    ],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    log.info(f"[sync] SMB connection successful to {host}")
                    return "smbclient"
            except Exception as e:
                log.debug(f"[sync] SMB failed: {e}")

        # 2. Try scp/sftp
        if shutil.which("sshpass") and shutil.which("scp"):
            try:
                ssh_port = qnap_config.get("ssh_port", 22)
                result = subprocess.run(
                    [
                        "sshpass", "-p", password,
                        "ssh", "-o", "StrictHostKeyChecking=no",
                        "-o", "ConnectTimeout=5",
                        "-p", str(ssh_port),
                        f"{username}@{host}",
                        "echo ok",
                    ],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    log.info(f"[sync] SSH connection successful to {host}")
                    return "scp"
            except Exception as e:
                log.debug(f"[sync] SSH failed: {e}")

        # 3. Try rclone (supports WebDAV, SFTP, SMB, etc.)
        if shutil.which("rclone"):
            log.info("[sync] rclone available — will use WebDAV")
            return "rclone"

        log.error(f"[sync] No upload method available for {host}")
        return None


class SyncJob(threading.Thread):
    """
    Background thread that uploads files to QNAP with progress reporting.
    Supports pause/resume/cancel.
    """

    def __init__(
        self,
        request_id: str,
        files: list,
        qnap_config: dict,
        options: dict,
        upload_method: str,
        ws_client=None,
    ):
        super().__init__(daemon=True, name=f"sync-{request_id}")
        self.request_id = request_id
        self.files = files
        self.qnap_config = qnap_config
        self.options = options
        self.upload_method = upload_method
        self.ws_client = ws_client

        self.files_uploaded = 0
        self.files_failed = 0
        self.bytes_uploaded = 0
        self.bytes_total = sum(f["size"] for f in files)
        self.failed_files = []
        self.deleted_files = 0

        self._paused = threading.Event()
        self._paused.set()  # Not paused initially
        self._cancelled = False
        self._start_time = None
        self._last_progress_report = 0

    def pause(self):
        log.info(f"[sync] Pausing sync (id={self.request_id})")
        self._paused.clear()

    def resume(self):
        log.info(f"[sync] Resuming sync (id={self.request_id})")
        self._paused.set()

    def cancel(self):
        log.info(f"[sync] Cancelling sync (id={self.request_id})")
        self._cancelled = True
        self._paused.set()  # Unblock if paused

    def run(self):
        """Main upload loop."""
        self._start_time = time.time()
        log.info(f"[sync] Upload started: {len(self.files)} files, {self.bytes_total / (1024**3):.2f} GB")

        for file_info in self.files:
            # Check for cancellation
            if self._cancelled:
                log.info("[sync] Upload cancelled by user.")
                break

            # Wait if paused
            self._paused.wait()
            if self._cancelled:
                break

            filepath = file_info["path"]
            relative = file_info["relative"]
            file_size = file_info["size"]

            try:
                success = self._upload_file(filepath, relative)
                if success:
                    self.files_uploaded += 1
                    self.bytes_uploaded += file_size

                    # Delete local file if option set
                    if self.options.get("delete_after_upload", False):
                        try:
                            os.remove(filepath)
                            self.deleted_files += 1
                        except OSError as e:
                            log.warning(f"[sync] Failed to delete {filepath}: {e}")
                else:
                    self.files_failed += 1
                    self.failed_files.append({"file": relative, "error": "Upload failed"})

            except Exception as e:
                self.files_failed += 1
                self.failed_files.append({"file": relative, "error": str(e)})
                log.error(f"[sync] Failed to upload {relative}: {e}")

            # Report progress periodically
            self._maybe_report_progress(relative)

        # Send completion event
        self._send_completion()

    def _upload_file(self, local_path: str, remote_relative: str) -> bool:
        """Upload a single file using the detected method."""
        host = self.qnap_config["host"]
        username = self.qnap_config["username"]
        password = self.qnap_config["password"]
        remote_path = self.qnap_config["remote_path"].rstrip("/")
        overwrite = self.options.get("overwrite_existing", False)

        remote_full = f"{remote_path}/{remote_relative}"
        remote_dir = os.path.dirname(remote_full)

        try:
            if self.upload_method == "smbclient":
                return self._upload_smb(local_path, remote_full, remote_dir)
            elif self.upload_method == "scp":
                return self._upload_scp(local_path, remote_full, remote_dir)
            elif self.upload_method == "rclone":
                return self._upload_rclone(local_path, remote_full)
            else:
                log.error(f"[sync] Unknown upload method: {self.upload_method}")
                return False
        except Exception as e:
            log.error(f"[sync] Upload error ({self.upload_method}): {e}")
            return False

    def _upload_smb(self, local_path: str, remote_path: str, remote_dir: str) -> bool:
        """Upload via smbclient."""
        host = self.qnap_config["host"]
        username = self.qnap_config["username"]
        password = self.qnap_config["password"]
        port = self.qnap_config.get("port", 445)
        share = self.qnap_config.get("share", "Recordings")

        # Create remote directory and upload
        commands = f"mkdir {remote_dir}\nput {local_path} {remote_path}\n"
        result = subprocess.run(
            [
                "smbclient", f"//{host}/{share}",
                "-U", f"{username}%{password}",
                "-p", str(port),
                "-c", f"mkdir {remote_dir}; put {local_path} {remote_path}",
            ],
            capture_output=True, text=True, timeout=300  # 5 min per file
        )
        return result.returncode == 0

    def _upload_scp(self, local_path: str, remote_path: str, remote_dir: str) -> bool:
        """Upload via scp (using sshpass for password auth)."""
        host = self.qnap_config["host"]
        username = self.qnap_config["username"]
        password = self.qnap_config["password"]
        ssh_port = self.qnap_config.get("ssh_port", 22)

        # Create remote directory
        subprocess.run(
            [
                "sshpass", "-p", password,
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-p", str(ssh_port),
                f"{username}@{host}",
                f"mkdir -p {remote_dir}",
            ],
            capture_output=True, timeout=30
        )

        # Upload file
        result = subprocess.run(
            [
                "sshpass", "-p", password,
                "scp", "-o", "StrictHostKeyChecking=no",
                "-P", str(ssh_port),
                local_path,
                f"{username}@{host}:{remote_path}",
            ],
            capture_output=True, text=True, timeout=600  # 10 min per file
        )
        return result.returncode == 0

    def _upload_rclone(self, local_path: str, remote_path: str) -> bool:
        """Upload via rclone using WebDAV backend."""
        host = self.qnap_config["host"]
        username = self.qnap_config["username"]
        password = self.qnap_config["password"]
        port = self.qnap_config.get("port", 443)
        protocol = self.qnap_config.get("protocol", "https")

        webdav_url = f"{protocol}://{host}:{port}"

        result = subprocess.run(
            [
                "rclone", "copyto", local_path, f":webdav:{remote_path}",
                f"--webdav-url={webdav_url}",
                f"--webdav-user={username}",
                f"--webdav-pass={password}",
                "--webdav-vendor=other",
                "--no-check-certificate",
                "-v",
            ],
            capture_output=True, text=True, timeout=600
        )
        return result.returncode == 0

    # ─── Progress Reporting ──────────────────────────────────────────────

    def _maybe_report_progress(self, current_file: str):
        """Send progress update if enough time has passed."""
        now = time.time()
        if now - self._last_progress_report < PROGRESS_REPORT_INTERVAL:
            return

        self._last_progress_report = now
        elapsed = now - self._start_time

        # Calculate speed
        speed_bps = self.bytes_uploaded / elapsed if elapsed > 0 else 0
        speed_mbps = round(speed_bps * 8 / (1024 * 1024), 1)

        # Estimate remaining time
        remaining_bytes = self.bytes_total - self.bytes_uploaded
        eta_seconds = int(remaining_bytes / speed_bps) if speed_bps > 0 else 0

        percent = round((self.bytes_uploaded / self.bytes_total) * 100, 1) if self.bytes_total > 0 else 0

        progress = {
            "request_id": self.request_id,
            "files_uploaded": self.files_uploaded,
            "files_total": len(self.files),
            "bytes_uploaded": self.bytes_uploaded,
            "bytes_total": self.bytes_total,
            "current_file": current_file,
            "speed_mbps": speed_mbps,
            "eta_seconds": eta_seconds,
            "percent": percent,
        }

        log.info(
            f"[sync] Progress: {self.files_uploaded}/{len(self.files)} files "
            f"({percent}%), {speed_mbps} Mbps, ETA {eta_seconds}s"
        )

        if self.ws_client:
            self.ws_client.send_sync("sync.progress", progress)

    def _send_completion(self):
        """Send sync completion event."""
        elapsed = time.time() - self._start_time
        duration_minutes = round(elapsed / 60, 1)

        status = "cancelled" if self._cancelled else "completed"
        uploaded_gb = round(self.bytes_uploaded / (1024 ** 3), 2)

        completion = {
            "request_id": self.request_id,
            "status": status,
            "files_uploaded": self.files_uploaded,
            "files_failed": self.files_failed,
            "total_uploaded_gb": uploaded_gb,
            "duration_minutes": duration_minutes,
            "failed_files": self.failed_files[:50],  # Limit to 50 entries
            "local_files_deleted": self.deleted_files,
        }

        log.info(
            f"[sync] Sync {status}: {self.files_uploaded} uploaded, "
            f"{self.files_failed} failed, {uploaded_gb} GB, {duration_minutes} min"
        )

        if self.ws_client:
            self.ws_client.send_sync("sync.complete", completion)
