#!/usr/bin/env python3
"""
RoadShield QNAP Background Sync Agent (v2.1.0)
==============================================
A daemon agent running permanently (e.g., inside Docker) on QNAP NAS.
Performs continuous periodic syncs of video recordings from the remote Laravel VPS,
downloads them securely using thread-pool concurrency with progress logs and SHA-256
verification, and triggers Laravel to clean up its local storage.
"""

import os
import sys
import time
import json
import logging
import threading
import hashlib
import shutil
from pathlib import Path
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# Version and globals
AGENT_VERSION = "2.1.0"
START_TIME = time.time()

# Paths setup
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_DIR = SCRIPT_DIR / "logs"
STATUS_PATH = SCRIPT_DIR / "status.json"

class ConfigManager:
    """Manages reading and validation of configuration settings from config.json."""
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = {}
        self.load()

    def load(self):
        if not self.config_path.exists():
            log_error_bootstrap(f"Configuration file not found at {self.config_path}. Please create it from config.json.example.")
            sys.exit(1)
        try:
            with open(self.config_path, "r") as f:
                self.config = json.load(f)
            self.validate()
        except json.JSONDecodeError as e:
            log_error_bootstrap(f"Failed to parse config.json: {str(e)}")
            sys.exit(1)

    def validate(self):
        required = ["server", "token", "download_path"]
        missing = [f for f in required if not self.config.get(f)]
        if missing:
            log_error_bootstrap(f"Missing required config fields: {', '.join(missing)}")
            sys.exit(1)

    @property
    def server(self) -> str:
        return self.config["server"].rstrip("/")

    @property
    def token(self) -> str:
        return self.config["token"]

    @property
    def download_path(self) -> Path:
        return Path(self.config["download_path"])

    @property
    def sync_interval(self) -> int:
        return int(self.config.get("sync_interval_seconds", 60))

    @property
    def max_concurrent_downloads(self) -> int:
        return int(self.config.get("max_concurrent_downloads", 3))

    @property
    def max_retries(self) -> int:
        return int(self.config.get("max_download_retries", 3))

    @property
    def retry_delay(self) -> int:
        return int(self.config.get("retry_delay_seconds", 5))

def log_error_bootstrap(msg: str):
    print(f"BOOTSTRAP ERROR: {msg}", file=sys.stderr)

# Setup logging function
def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "agent.log"
    
    logger = logging.getLogger("qnap-agent")
    logger.setLevel(logging.INFO)
    logger.handlers = []

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

def calculate_sha256(file_path: Path) -> str:
    """Calculates the SHA-256 checksum of a local file in chunks."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

class LaravelClient:
    """Handles REST communications with the remote Laravel API."""
    def __init__(self, server: str, token: str):
        self.server = server
        self.token = token

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"
        }

    def fetch_jetsons(self) -> list:
        url = f"{self.server}/api/surveillance/recordings/browse"
        response = requests.get(url, headers=self.headers, timeout=30)
        response.raise_for_status()
        return response.json().get("jetsons", [])

    def fetch_recordings(self, jetson_name: str) -> list:
        url = f"{self.server}/api/surveillance/recordings/browse/{jetson_name}"
        response = requests.get(url, headers=self.headers, timeout=30)
        response.raise_for_status()
        return response.json().get("recordings", [])

    def download_file_chunk(self, download_url: str, temp_path: Path, expected_size: int, progress_callback=None):
        # Connect timeout = 30s, Read timeout = None (wait indefinitely for data to flow)
        with requests.get(download_url, headers=self.headers, stream=True, timeout=(30, None)) as r:
            r.raise_for_status()
            with open(temp_path, "wb") as f:
                bytes_written = 0
                for chunk in r.iter_content(chunk_size=1024 * 1024): # 1MB
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
                        if progress_callback:
                            progress_callback(bytes_written, expected_size)

    def notify_download_complete(self, jetson_name: str, relative_path: str) -> bool:
        url = f"{self.server}/api/surveillance/recordings/download-complete"
        payload = {
            "jetson_name": jetson_name,
            "relative_path": relative_path
        }
        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=15)
            if response.status_code == 200:
                return True
            log.warning(f"Failed to notify download-complete for {relative_path} (HTTP {response.status_code}): {response.text}")
            return False
        except Exception as e:
            log.error(f"Error notifying download-complete for {relative_path}: {str(e)}")
            return False

# Global singletons placeholder (initialized in main)
log = None
config_manager = None
client = None

class DownloadJob:
    def __init__(self, jetson_name: str, camera_id: str, date_str: str, file_name: str, download_url: str, size: int, sha256: str):
        self.jetson_name = jetson_name
        self.camera_id = camera_id
        self.date_str = date_str
        self.file_name = file_name
        self.download_url = download_url
        self.size = size
        self.sha256 = sha256

    @property
    def relative_path(self) -> str:
        return f"{self.camera_id}/{self.date_str}/{self.file_name}"

    @property
    def unique_id(self) -> str:
        return f"{self.jetson_name}/{self.relative_path}"

class SyncEngine:
    """Manages lifecycle of scanning files, queuing them, and running concurrent downloaders."""
    def __init__(self):
        self.running = True
        self.active_downloads = set()
        self._downloads_lock = threading.Lock()
        
        # In-memory retry queue for failed notifications
        self.failed_notifications = []
        self._notif_lock = threading.Lock()

        # Monitoring stats
        self.last_sync_time = 0
        self.last_sync_status = "unknown"
        self.last_error = None

    def update_status(self, status: str):
        """Dumps current runtime statistics to status.json for dashboard health checks."""
        try:
            total, used, free = shutil.disk_usage(config_manager.download_path)
        except Exception:
            free = 0
            
        with self._downloads_lock:
            current_active = list(self.active_downloads)

        status_data = {
            "agent_version": AGENT_VERSION,
            "status": status,
            "uptime_seconds": int(time.time() - START_TIME),
            "last_sync_time": self.last_sync_time,
            "last_sync_status": self.last_sync_status,
            "last_error": self.last_error,
            "free_disk_space_bytes": free,
            "free_disk_space_gb": round(free / (1024 ** 3), 2),
            "current_downloads": current_active,
            "pending_notifications_count": len(self.failed_notifications)
        }
        try:
            with open(STATUS_PATH, "w") as f:
                json.dump(status_data, f, indent=2)
        except Exception as e:
            if log:
                log.error(f"Failed to update status.json: {str(e)}")

    def process_download(self, job: DownloadJob) -> bool:
        """Executes a single download task with SHA-256 verification and retries."""
        local_file = config_manager.download_path / job.jetson_name / job.camera_id / job.date_str / job.file_name
        local_file.parent.mkdir(parents=True, exist_ok=True)

        # 1. Skip if file already exists with correct size
        # (Avoids reading heavy hashes of already-synced files for performance)
        if local_file.exists() and local_file.stat().st_size == job.size:
            log.debug(f"File already verified locally: {job.unique_id}")
            # Ensure remote copy is cleared
            self.trigger_laravel_cleanup(job)
            return True

        with self._downloads_lock:
            self.active_downloads.add(job.unique_id)
        self.update_status("syncing")

        temp_file = local_file.with_suffix(".tmp")
        max_retries = config_manager.max_retries
        retry_delay = config_manager.retry_delay
        download_success = False

        try:
            for attempt in range(1, max_retries + 1):
                log.info(f"Attempt {attempt}/{max_retries} - Downloading: {job.unique_id} ({job.size / (1024*1024):.2f} MB)")
                
                # Setup progress reporter
                last_logged_pct = [-10] # list wrapper to allow modification inside scope
                def progress_cb(written, total):
                    if total > 0:
                        pct = int((written / total) * 100)
                        # Log every 20% to avoid log noise
                        if pct >= last_logged_pct[0] + 20 or pct == 100:
                            last_logged_pct[0] = pct
                            log.info(f"Progress [{job.file_name}]: {pct}% ({written / (1024*1024):.2f} MB)")

                try:
                    client.download_file_chunk(job.download_url, temp_file, job.size, progress_cb)
                    
                    # 2. Check File Integrity using SHA-256
                    log.info(f"Verifying SHA-256 checksum for {job.file_name}...")
                    local_hash = calculate_sha256(temp_file)
                    
                    if local_hash == job.sha256:
                        if local_file.exists():
                            local_file.unlink()
                        temp_file.rename(local_file)
                        log.info(f"Verification successful: {job.unique_id}")
                        download_success = True
                        break
                    else:
                        log.error(f"SHA-256 mismatch for {job.file_name}. Expected {job.sha256}, got {local_hash}")
                        if temp_file.exists():
                            temp_file.unlink()
                except Exception as e:
                    log.error(f"Download error on attempt {attempt} for {job.unique_id}: {str(e)}")
                    if temp_file.exists():
                        temp_file.unlink()
                
                if attempt < max_retries:
                    log.info(f"Waiting {retry_delay}s before retrying...")
                    time.sleep(retry_delay)

            if download_success:
                self.trigger_laravel_cleanup(job)
                return True
            else:
                log.error(f"Failed to sync file after {max_retries} attempts: {job.unique_id}")
                return False
        finally:
            with self._downloads_lock:
                self.active_downloads.discard(job.unique_id)
            self.update_status("idle" if not self.active_downloads else "syncing")

    def trigger_laravel_cleanup(self, job: DownloadJob):
        """Triggers VPS Laravel server cleanup. Queues notification on connection failure."""
        success = client.notify_download_complete(job.jetson_name, job.relative_path)
        if success:
            log.info(f"Laravel notified. VPS cleaned up for: {job.unique_id}")
        else:
            log.warning(f"Notification failed. Queueing cleanup retry for: {job.unique_id}")
            with self._notif_lock:
                # Add to queue if not already present
                item = (job.jetson_name, job.relative_path)
                if item not in self.failed_notifications:
                    self.failed_notifications.append(item)

    def process_pending_notifications(self):
        """Retries all failed download completion API requests."""
        with self._notif_lock:
            if not self.failed_notifications:
                return
            log.info(f"Retrying {len(self.failed_notifications)} pending Laravel delete notifications...")
            still_failing = []
            
            for jetson_name, rel_path in self.failed_notifications:
                success = client.notify_download_complete(jetson_name, rel_path)
                if success:
                    log.info(f"Retry successful. VPS cleaned up for: {jetson_name}/{rel_path}")
                else:
                    still_failing.append((jetson_name, rel_path))
            
            self.failed_notifications = still_failing

    def run_sync_cycle(self):
        """Scans Laravel VPS for recordings and process them concurrently."""
        log.info("Scanning remote recordings...")
        self.update_status("syncing")
        
        # Retry any notifications that failed previously
        self.process_pending_notifications()

        try:
            jetsons = client.fetch_jetsons()
        except Exception as e:
            self.last_error = f"Failed to scan remote Jetsons: {str(e)}"
            log.error(self.last_error)
            self.last_sync_status = "failed"
            self.update_status("idle")
            return

        jobs = []

        # Gather all pending downloads
        for jetson in jetsons:
            jetson_name = jetson.get("name")
            try:
                recordings = client.fetch_recordings(jetson_name)
            except Exception as e:
                log.error(f"Failed to fetch files for jetson {jetson_name}: {str(e)}")
                continue

            for cam_rec in recordings:
                camera_id = cam_rec.get("camera")
                for date_info in cam_rec.get("dates", []):
                    date_str = date_info.get("date")
                    for file_info in date_info.get("files", []):
                        file_name = file_info.get("name")
                        size = file_info.get("size_bytes")
                        sha256 = file_info.get("sha256")
                        download_uri = file_info.get("download_url")
                        download_url = f"{client.server}{download_uri}"

                        job = DownloadJob(
                            jetson_name=jetson_name,
                            camera_id=camera_id,
                            date_str=date_str,
                            file_name=file_name,
                            download_url=download_url,
                            size=size,
                            sha256=sha256
                        )
                        
                        # Add if local file is missing or has wrong size
                        local_file = config_manager.download_path / job.jetson_name / job.camera_id / job.date_str / job.file_name
                        if not local_file.exists() or local_file.stat().st_size != job.size:
                            jobs.append(job)

        if not jobs:
            log.info("All files synchronized. QNAP is up to date.")
            self.last_sync_time = int(time.time())
            self.last_sync_status = "success"
            self.last_error = None
            self.update_status("idle")
            return

        log.info(f"Sync batch size: {len(jobs)} files pending download.")

        # Process queue concurrently
        workers = config_manager.max_concurrent_downloads
        log.info(f"Starting downloads with {workers} concurrent workers.")
        
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="Downloader") as executor:
            future_to_job = {executor.submit(self.process_download, job): job for job in jobs}
            
            success_count = 0
            fail_count = 0
            
            for future in as_completed(future_to_job):
                job = future_to_job[future]
                try:
                    success = future.result()
                    if success:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    log.error(f"Worker exception processing job {job.unique_id}: {str(e)}")
                    fail_count += 1

            self.last_sync_time = int(time.time())
            self.last_sync_status = "success" if fail_count == 0 else "partial_failure"
            self.last_error = None if fail_count == 0 else f"{fail_count} files failed to download during sync cycle."
            log.info(f"Sync cycle batch complete. Successful: {success_count}, Failed: {fail_count}")
            self.update_status("idle")

    def start(self):
        log.info("Starting background agent loop.")
        while self.running:
            try:
                self.run_sync_cycle()
            except Exception as e:
                self.last_error = f"Unhandled exception in sync cycle: {str(e)}"
                self.last_sync_status = "failed"
                log.critical(self.last_error)
            
            log.info(f"Sleeping for {config_manager.sync_interval} seconds before next sync...")
            
            sleep_timer = 0
            while sleep_timer < config_manager.sync_interval and self.running:
                time.sleep(1)
                sleep_timer += 1

    def stop(self):
        log.info("Stopping QNAP agent...")
        self.running = False

def main():
    global log, config_manager, client
    
    # 1. Initialize configurations
    config_manager = ConfigManager(CONFIG_PATH)

    # 2. Setup persistent rotating loggers
    log = setup_logging(LOG_DIR)
    
    # 3. Prevent duplicate instances using fcntl file locks
    lock_file_path = SCRIPT_DIR / "agent.lock"
    try:
        import fcntl
        # Keep lock_file alive for the duration of the process
        global lock_file
        lock_file = open(lock_file_path, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        # Fallback for systems without fcntl (e.g. Windows during development)
        log.warning("fcntl module not available. Instance locking bypassed.")
    except (IOError, OSError):
        log_error_bootstrap("Another instance of the RoadShield QNAP Agent is already running. Exiting.")
        sys.exit(1)

    log.info("====================================================")
    log.info(f"RoadShield QNAP Background Agent Initializing v{AGENT_VERSION}")
    log.info("====================================================")
    
    log.info(f"Server URL: {config_manager.server}")
    log.info(f"Local Storage Path: {config_manager.download_path}")
    log.info(f"Sync Interval: {config_manager.sync_interval}s")
    log.info(f"Concurrent workers: {config_manager.max_concurrent_downloads}")

    # 4. Setup Laravel HTTP client
    client = LaravelClient(config_manager.server, config_manager.token)

    # 5. Run loop
    engine = SyncEngine()
    
    # Handle shutdown signals
    import signal
    def handle_signal(signum, frame):
        log.info(f"Received signal {signum}. Shutting down cleanly...")
        engine.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    engine.start()
    
    # Cleanup status output on exit
    if STATUS_PATH.exists():
        try:
            STATUS_PATH.unlink()
        except Exception:
            pass
    log.info("QNAP Agent has shut down.")

if __name__ == "__main__":
    main()
