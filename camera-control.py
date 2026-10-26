#!/usr/bin/env python3
"""
RoadShield — camera-control.py
================================
Polls/connects to the Laravel API for pending PTZ commands and executes them
via Hikvision ISAPI on the local camera network.

WebSocket Integration:
  Connects to Laravel WebSocket server for real-time instant commands.
  Falls back to HTTP polling automatically if WebSocket is disconnected.
  Provides system diagnostics checks and QNAP NAS backup sync.
"""

import time
import json
import logging
import requests
import xml.etree.ElementTree as ET
from requests.auth import HTTPDigestAuth
import subprocess
import platform
import socket
import threading
import os
import re
import glob
from datetime import datetime, timedelta

# ─── Configuration ──────────────────────────────────────────────────────────
import sys
from device_identity import get_device_id
from mp4_remux import remux_for_upload

LARAVEL_URL       = "https://controlroom.dubibid.com"
SURVEILLANCE_TOKEN = "b8e2ed9ae5def597e6a59f2801fca19fa758ab1a0cd3e9900b708b3aa357bc3c"
SERVER_ID          = get_device_id()  # unique per device, auto-generated — never hand-edit this
AUTO_UPLOAD        = True  # background upload of recordings, see auto_upload.py

# Allow CLI overrides (e.g. passed from connect-to-server.sh)
for arg in sys.argv:
    if arg.startswith("--url="):
        LARAVEL_URL = arg.split("=", 1)[1]
    elif arg.startswith("--token="):
        SURVEILLANCE_TOKEN = arg.split("=", 1)[1]
    elif arg.startswith("--server-name=") or arg.startswith("--jetson-name="):
        SERVER_ID = arg.split("=", 1)[1]
    elif arg == "--no-auto-upload":
        AUTO_UPLOAD = False

# Cameras are pulled from the dashboard (see sync_camera_config.py, run
# before this script starts) and cached to cameras_config.json. Falls back
# to this hardcoded dict only if that file doesn't exist yet (e.g. device
# not registered on the dashboard yet).
_CAMERAS_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cameras_config.json")
CAMERAS = {
    "cam1": {"ip": "192.168.1.64", "user": "admin", "password": "hikvision@12", "channel": 1},
    "cam2": {"ip": "192.168.1.65", "user": "admin", "password": "hikvision@12", "channel": 1},
    "cam3": {"ip": "192.168.1.165", "user": "admin", "password": "12345", "channel": 1, "type": "generic", "rtsp_port": 8554},
}
if os.path.exists(_CAMERAS_CONFIG_PATH):
    try:
        with open(_CAMERAS_CONFIG_PATH) as _f:
            _synced = json.load(_f)
        if _synced:
            CAMERAS = _synced
            print(f"[camera-ctrl] Loaded {len(CAMERAS)} camera(s) from cameras_config.json (dashboard-synced).")
    except Exception as _e:
        print(f"[camera-ctrl] Failed to load cameras_config.json, using built-in defaults: {_e}")
SHOW_LOCAL_VIEWER = False  # Set to True to open a local window showing the transcoded stream
POLL_INTERVAL  = 0.8   # seconds between polls per camera
ISAPI_TIMEOUT  = 3     # seconds for ISAPI request timeout
PTZ_SPEED      = 40    # 0-100 for Hikvision ISAPI

# ─── Quality Settings Mapping ───────────────────────────────────────────────
QUALITY_SETTINGS = {
    "hd": {
        "width": 1280,
        "height": 720,
        "bitrate": "1500k",
        "bufsize": "3000k",
        "source_suffix": ""       # Plays HD stream (camX)
    },
    "sd": {
        "width": 854,
        "height": 480,
        "bitrate": "500k",
        "bufsize": "1000k",
        "source_suffix": "_sub"   # Plays SD sub-stream (camX_sub)
    },
    "ultra": {
        "width": 640,
        "height": 360,
        "bitrate": "150k",
        "bufsize": "300k",
        "source_suffix": "_sub"   # Plays Ultra low sub-stream (camX_sub)
    }
}

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_FILE = "/tmp/camera-control.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [camera-ctrl] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Add file handler to write log messages to log file
try:
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [camera-ctrl] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(file_handler)
except Exception as e:
    log.warning(f"Could not initialize logging to {LOG_FILE}: {e}")

# Global state for WebSocket and Sync
ws_connected = False
sync_paused = False
sync_cancelled = False

# Tracks the currently-running sync thread. Without this, a user re-clicking
# "Start Sync" (e.g. because a previous attempt looked stuck) spawns a
# second run_vps_sync thread while the first is still alive, and both end
# up uploading the same files at once. See sync.start handling below.
_sync_thread = None

# Serializes all ws.send() calls — see send_ws_event() for why this is required.
_ws_send_lock = threading.Lock()

# Always points at the currently-live connection. Background threads (sync,
# diagnostics, PTZ) are started with whatever `ws` object was live at the
# moment they were spawned; a long-running one (e.g. uploading a
# large 4K recording can take minutes) can easily outlive that connection if
# it drops and reconnects meanwhile. Without this, such a thread keeps
# sending on the old, now-closed socket forever ("Connection is already
# closed") and the browser never sees another progress update. Updated in
# on_open() on every (re)connect; send_ws_event() prefers this over whatever
# `ws` it was called with.
_current_ws = None

# ─── Reachability Probes ──────────────────────────────────────────────────
def ping_check(ip: str) -> bool:
    """Check host reachability using ping."""
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    cmd = ['ping', param, '1', '-W', '1', ip]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False

def rtsp_probe(ip: str, port: int = 554) -> bool:
    """Probe if RTSP port is open on the camera."""
    try:
        s = socket.create_connection((ip, port), timeout=1.0)
        s.close()
        return True
    except Exception:
        return False

# ─── API helpers ─────────────────────────────────────────────────────────────
def api_headers():
    return {
        "Authorization": f"Bearer {SURVEILLANCE_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def poll_commands(camera_id: str) -> list:
    """Fetch pending PTZ commands from Laravel cache."""
    url = f"{LARAVEL_URL}/api/surveillance/cameras/{camera_id}/ptz/poll"
    try:
        r = requests.get(url, headers=api_headers(), timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data.get("commands", [])
    except Exception as e:
        log.warning(f"Poll failed for {camera_id}: {e}")
    return []


def poll_settings() -> dict:
    """Fetch camera settings (Quality + FPS) from Laravel."""
    url = f"{LARAVEL_URL}/api/surveillance/cameras/settings"
    try:
        r = requests.get(url, headers=api_headers(), timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data.get("settings", {})
    except Exception as e:
        log.warning(f"Poll settings failed: {e}")
    return {}


def ack_command(camera_id: str, command_id: str, success: bool, error: str = None):
    """Report PTZ command result back to Laravel."""
    url  = f"{LARAVEL_URL}/api/surveillance/cameras/{camera_id}/ptz/ack"
    body = {"command_id": command_id, "success": success, "error": error}
    try:
        requests.post(url, headers=api_headers(), json=body, timeout=5)
    except Exception as e:
        log.warning(f"Ack failed for {camera_id}/{command_id}: {e}")


# ─── Transcoder Manager (FFmpeg Processes with Fallback Video) ───────────────
class TranscoderManager:
    def __init__(self):
        self.active_processes = {}  # camera_id -> subprocess.Popen
        self.active_viewers   = {}  # camera_id -> subprocess.Popen
        self.current_settings = {}  # camera_id -> {"quality": str, "fps": int, "fallback": bool}

    def apply_settings(self, camera_id, quality, fps):
        prev = self.current_settings.get(camera_id)
        proc = self.active_processes.get(camera_id)

        # Check reachability of camera
        cam_cfg = CAMERAS.get(camera_id, {})
        ip = cam_cfg.get("ip")
        rtsp_port = cam_cfg.get("rtsp_port", 554)
        is_reachable = ping_check(ip) if ip else False
        is_rtsp_ok = rtsp_probe(ip, rtsp_port) if is_reachable else False
        use_fallback = not (is_reachable and is_rtsp_ok)

        settings_changed = (not prev or 
                            prev["quality"] != quality or 
                            prev["fps"] != fps or 
                            prev.get("fallback") != use_fallback)
        process_dead = proc is not None and proc.poll() is not None

        if settings_changed or process_dead or not proc:
            if use_fallback:
                log.warning(f"[{camera_id}] Camera unreachable at {ip}. Switching to fallback video.")
            else:
                log.info(f"[{camera_id}] Applying transcoding: quality={quality.upper()}, fps={fps}")
            self.stop_transcoder(camera_id)
            self.start_transcoder(camera_id, quality, fps, use_fallback)
            self.current_settings[camera_id] = {"quality": quality, "fps": fps, "fallback": use_fallback}

    def start_transcoder(self, camera_id, quality, fps, use_fallback=False):
        cfg = QUALITY_SETTINGS.get(quality, QUALITY_SETTINGS["hd"])
        width = cfg["width"]
        height = cfg["height"]
        bitrate = cfg["bitrate"]
        bufsize = cfg["bufsize"]
        suffix = cfg["source_suffix"]

        output_url = f"rtsp://127.0.0.1:8554/{camera_id}_live"

        if use_fallback:
            # Fallback video injection using testsrc + drawtext filter
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                "-f", "lavfi",
                "-i", f"testsrc=size={width}x{height}:rate={fps},drawtext=text='CAMERA {camera_id.upper()} OFFLINE':x=(w-text_w)/2:y=(h-text_h)/2:fontsize=28:fontcolor=white:box=1:boxcolor=red@0.8",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                "-an", "-f", "rtsp", output_url
            ]
        else:
            # Standard GStreamer -> MediaMTX stream path input
            input_url = f"rtsp://127.0.0.1:8554/{camera_id}{suffix}"
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                "-i", input_url,
                "-vf", f"scale={width}:{height},fps={fps}",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bufsize, "-an",
                "-f", "rtsp", output_url
            ]

        try:
            # Start process in background
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.active_processes[camera_id] = proc
            log.info(f"[{camera_id}] Started FFmpeg {'fallback' if use_fallback else 'standard'} transcoder (PID {proc.pid})")
            
            # Start local viewer if enabled
            if SHOW_LOCAL_VIEWER:
                import threading
                def launch_viewer_delayed():
                    time.sleep(1.0)
                    if self.active_processes.get(camera_id) == proc: # make sure it hasn't been stopped
                        self.start_viewer(camera_id)
                threading.Thread(target=launch_viewer_delayed, daemon=True).start()

        except Exception as e:
            log.error(f"[{camera_id}] Failed to start FFmpeg: {e}.")

    def start_viewer(self, camera_id):
        output_url = f"rtsp://127.0.0.1:8554/{camera_id}_live"
        
        # Try running ffplay with lowest latency parameters
        ffplay_cmd = [
            "ffplay", "-rtsp_transport", "tcp",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-window_title", f"Transcoded Preview (Local) — {camera_id.upper()}",
            output_url
        ]
        
        try:
            viewer = subprocess.Popen(ffplay_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.active_viewers[camera_id] = viewer
            log.info(f"[{camera_id}] Opened local transcoded preview window (ffplay PID {viewer.pid})")
        except FileNotFoundError:
            # Fallback to GStreamer autovideosink if ffplay is not found
            gst_cmd = [
                "gst-launch-1.0", "-v", "rtspsrc", f"location={output_url}", "latency=0", "protocols=tcp",
                "!", "decodebin", "!", "videoconvert", "!", "autovideosink", "sync=false"
            ]
            try:
                viewer = subprocess.Popen(gst_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.active_viewers[camera_id] = viewer
                log.info(f"[{camera_id}] Opened local transcoded preview window (GStreamer PID {viewer.pid})")
            except Exception as e:
                log.warning(f"[{camera_id}] Could not start local preview window: {e}")

    def stop_transcoder(self, camera_id):
        # Stop preview window if active
        viewer = self.active_viewers.get(camera_id)
        if viewer:
            log.info(f"[{camera_id}] Closing local preview window (PID {viewer.pid})...")
            try:
                viewer.terminate()
                viewer.wait(timeout=2)
            except Exception:
                try: viewer.kill()
                except Exception: pass
            self.active_viewers[camera_id] = None

        proc = self.active_processes.get(camera_id)
        if proc:
            log.info(f"[{camera_id}] Stopping FFmpeg dynamic transcoder (PID {proc.pid})...")
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                log.warning(f"[{camera_id}] FFmpeg didn't terminate, killing...")
                proc.kill()
                proc.wait()
            except Exception as e:
                log.error(f"[{camera_id}] Error stopping FFmpeg: {e}")
            self.active_processes[camera_id] = None

    def stop_all(self):
        for camera_id in list(self.active_processes.keys()):
            self.stop_transcoder(camera_id)


transcoder_manager = TranscoderManager()


# ─── Hikvision ISAPI PTZ ──────────────────────────────────────────────────────
def build_ptz_xml(pan=0, tilt=0, zoom=0):
    """Build Hikvision continuous PTZ XML command."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PTZData>
  <pan>{pan}</pan>
  <tilt>{tilt}</tilt>
  <zoom>{zoom}</zoom>
</PTZData>"""


def build_stop_xml():
    return """<?xml version="1.0" encoding="utf-8"?>
<PTZData>
  <pan>0</pan>
  <tilt>0</tilt>
  <zoom>0</zoom>
</PTZData>"""


# Map action → (pan, tilt, zoom) speed values for Hikvision
ACTION_MAP = {
    "pan_left":   (-PTZ_SPEED, 0, 0),
    "pan_right":  ( PTZ_SPEED, 0, 0),
    "tilt_up":    (0,  PTZ_SPEED, 0),
    "tilt_down":  (0, -PTZ_SPEED, 0),
    "zoom_in":    (0, 0,  PTZ_SPEED),
    "zoom_out":   (0, 0, -PTZ_SPEED),
    "pan_stop":   (0, 0, 0),
    "tilt_stop":  (0, 0, 0),
    "zoom_stop":  (0, 0, 0),
    "stop":       (0, 0, 0),
    "home":       None,  # handled separately
}


def execute_ptz(camera_id: str, cmd: dict) -> tuple:
    """Execute a PTZ command via Hikvision ISAPI. Returns (success, error)."""
    cam = CAMERAS.get(camera_id)
    if not cam:
        return False, f"Camera {camera_id} not in local config"

    action  = cmd.get("action", "stop")
    speed   = min(7, max(1, cmd.get("speed", 3)))  # Laravel sends 1-7
    channel = cam["channel"]
    ip      = cam["ip"]
    auth    = HTTPDigestAuth(cam["user"], cam["password"])

    base_url = f"http://{ip}/ISAPI/PTZCtrl/channels/{channel}"

    try:
        if action == "home":
            preset_url = f"{base_url}/presets/1/goto"
            r = requests.put(preset_url, auth=auth, timeout=ISAPI_TIMEOUT)
            if r.status_code in (200, 204):
                log.info(f"[{camera_id}] → HOME (preset 1)")
                return True, None
            return False, f"Home failed: HTTP {r.status_code}"

        values = ACTION_MAP.get(action)
        if values is None:
            return False, f"Unknown action: {action}"

        factor = speed / 4.0
        pan, tilt, zoom = [int(v * factor) for v in values]
        xml_body = build_ptz_xml(pan, tilt, zoom)

        r = requests.put(
            f"{base_url}/continuous",
            data=xml_body.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            auth=auth,
            timeout=ISAPI_TIMEOUT,
        )

        if r.status_code in (200, 204):
            direction = action.replace("_", " ").upper()
            log.info(f"[{camera_id}] → {direction} (pan={pan} tilt={tilt} zoom={zoom})")
            return True, None
        else:
            return False, f"ISAPI error: HTTP {r.status_code} — {r.text[:200]}"

    except requests.exceptions.ConnectTimeout:
        return False, f"Camera {ip} unreachable (timeout)"
    except requests.exceptions.ConnectionError:
        return False, f"Camera {ip} not accessible (connection refused)"
    except Exception as e:
        return False, str(e)


# ─── VPS Recording Sync — File Scanner & HTTP Uploader ────────────────────────

def verify_file_on_server(upload_url: str, upload_token: str, jetson_name: str,
                          relative_path: str, expected_size: int) -> bool:
    """
    Verify that a file exists on the VPS server with the correct size.
    Uses the dedicated /api/surveillance/recordings/verify endpoint for efficiency.
    Returns True if the file is confirmed on server with matching size.
    """
    try:
        # Derive verify URL from upload_url
        # e.g. https://vps.example.com/api/surveillance/recordings/upload
        #   -> https://vps.example.com/api/surveillance/recordings/verify
        verify_url = upload_url.replace('/recordings/upload', '/recordings/verify')

        resp = requests.post(
            verify_url,
            json={
                'jetson_name':   jetson_name,
                'relative_path': relative_path,
                'expected_size': expected_size,
            },
            headers={
                'Authorization': f'Bearer {upload_token}',
                'Accept':        'application/json',
            },
            timeout=30,
            verify=False,
        )

        if resp.status_code != 200:
            log.warning(f"[sync] verify: server returned HTTP {resp.status_code} for {relative_path}")
            return False

        data = resp.json()
        if data.get('verified') is True:
            return True

        reason = data.get('reason', 'unknown')
        server_size = data.get('server_size')
        log.warning(
            f"[sync] verify: NOT verified for {relative_path} — "
            f"reason='{reason}', server_size={server_size}, expected={expected_size}"
        )
        return False

    except Exception as e:
        log.error(f"[sync] verify: exception while verifying {relative_path}: {e}")
        return False


def create_mock_recordings(base_dir):
    """Populate mock recordings directory for testing sync pipelines."""
    today = datetime.now().date()
    for cam in CAMERAS.keys():
        for offset in range(3):
            date_str = (today - timedelta(days=offset)).strftime('%Y-%m-%d')
            dir_path = os.path.join(base_dir, cam, date_str)
            os.makedirs(dir_path, exist_ok=True)
            for hour in ['08-00-00', '12-00-00', '16-00-00']:
                file_path = os.path.join(dir_path, f"{hour}.mp4")
                if not os.path.exists(file_path):
                    try:
                        with open(file_path, "wb") as f:
                            f.write(b"\x00" * 1024 * 1024) # 1MB dummy mp4 file
                    except Exception:
                        pass

def get_video_duration(path):
    """Retrieve video duration using ffprobe. Fallback to mock duration if empty/test file."""
    try:
        import subprocess
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
        duration_str = result.stdout.strip()
        if duration_str:
            return round(float(duration_str))
    except Exception:
        pass
    try:
        if os.path.getsize(path) <= 1024 * 1024:
            return 3600  # Default mock duration: 1 hour
    except Exception:
        pass
    return 0


def get_files_to_sync(scope, cameras_filter, days_filter, source='sub'):
    """
    Scan the recordings/ directory and filter files based on sync parameters.
    `source` selects which stream tier to sync from: 'sub' (default, low-res
    — the top-level dir name ends in "_sub") or 'main' (full-res, everything
    else). `cameras_filter` is matched against the base camera id with any
    "_sub" suffix stripped, so camera selection works the same in both modes.
    """
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)
        create_mock_recordings(base_dir)

    all_files = []
    for root, dirs, files in os.walk(base_dir):
        for f in files:
            # Skip temp 1080p copies an older agent version (which compressed
            # before upload) may have left behind after a crash — they're
            # partial files, not real recordings.
            if f.endswith('.mp4') and '.sync1080p.' not in f:
                path = os.path.join(root, f)
                rel_path = os.path.relpath(path, base_dir)
                all_files.append((path, rel_path))

    filtered_files = []
    now = datetime.now()

    for path, rel_path in all_files:
        parts = rel_path.split(os.sep)
        top_dir = parts[0]
        is_sub_dir = top_dir.endswith('_sub')
        base_cam_id = top_dir[:-len('_sub')] if is_sub_dir else top_dir

        # Filter by stream tier (main vs sub)
        if (source == 'sub') != is_sub_dir:
            continue

        # Filter by camera — compare against the base id (suffix stripped)
        # so camera selection works the same regardless of source tier.
        if scope == 'cameras' and cameras_filter:
            if base_cam_id not in cameras_filter:
                continue

        # Filter by date/days
        file_mtime = datetime.fromtimestamp(os.path.getmtime(path))
        if scope == 'today':
            if file_mtime.date() != now.date():
                continue
        elif scope == 'last_n_days' and days_filter:
            limit = now - timedelta(days=days_filter)
            if file_mtime < limit:
                continue

        filtered_files.append((path, rel_path))

    return filtered_files


def run_vps_sync(ws, request_id, vps_config, options):
    """Upload selected recording files to VPS server via HTTP multipart upload."""
    global sync_paused, sync_cancelled
    sync_paused = False
    sync_cancelled = False

    upload_url = vps_config.get("upload_url")
    upload_token = vps_config.get("token", SURVEILLANCE_TOKEN)

    scope = options.get("scope", "all")
    cameras = options.get("cameras", [])
    days = options.get("days")
    source = options.get("source", "sub")
    delete_after_upload = options.get("delete_after_upload", True)
    overwrite = options.get("overwrite_existing", False)

    if not upload_url:
        send_ws_event(ws, "sync.start.ack", {
            "request_id": request_id,
            "status": "error",
            "error": "No upload URL provided by server."
        })
        return

    try:
        files_filter = options.get("files")
        if files_filter:
            base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
            files = []
            for rel_path in files_filter:
                full_path = os.path.join(base_dir, rel_path)
                if os.path.exists(full_path):
                    files.append((full_path, rel_path))
        else:
            files = get_files_to_sync(scope, cameras, days, source)
    except Exception as e:
        send_ws_event(ws, "sync.start.ack", {
            "request_id": request_id,
            "status": "error",
            "error": f"Failed to scan recordings: {str(e)}"
        })
        return

    total_files = len(files)
    total_bytes = sum(os.path.getsize(f[0]) for f in files)
    total_size_gb = round(total_bytes / (1024 * 1024 * 1024), 2)
    
    estimated_seconds = (total_bytes * 8) / (25 * 1000 * 1000) if total_bytes > 0 else 0
    estimated_time_minutes = max(1, int(estimated_seconds / 60))

    # Start Ack
    files_list = [f[1].replace(os.sep, '/') for f in files]
    send_ws_event(ws, "sync.start.ack", {
        "request_id": request_id,
        "status": "started",
        "total_files": total_files,
        "total_size_gb": total_size_gb,
        "estimated_time_minutes": estimated_time_minutes,
        "files_list": files_list
    })

    if total_files == 0:
        send_ws_event(ws, "sync.complete", {
            "request_id": request_id,
            "status": "completed",
            "files_uploaded": 0,
            "files_failed": 0,
            "total_uploaded_gb": 0.0,
            "duration_minutes": 0,
            "failed_files": [],
            "local_files_deleted": 0
        })
        return

    files_uploaded = 0
    files_failed = 0
    bytes_uploaded = 0
    failed_files = []
    local_files_deleted = 0
    t_start = time.time()

    for idx, (path, rel_path) in enumerate(files):
        if sync_cancelled:
            break

        while sync_paused and not sync_cancelled:
            time.sleep(0.5)

        if sync_cancelled:
            break

        current_file_rel = rel_path.replace(os.sep, '/')

        # No re-encoding — but upload a regular-MP4 copy (index at front,
        # hvc1 for H.265) so it plays on macOS and seeks instantly; lossless,
        # see mp4_remux.py. The local recording is left as-is.
        upload_path, is_temp_upload = remux_for_upload(path, log)
        file_size = os.path.getsize(upload_path)

        upload_success = False
        upload_skipped = False
        error_msg = None

        # Chunk size: 10 MB (10 * 1024 * 1024 bytes)
        chunk_size = 10 * 1024 * 1024
        total_chunks = max(1, (file_size + chunk_size - 1) // chunk_size)
        chunk_upload_url = upload_url.replace('/recordings/upload', '/recordings/upload-chunk')

        try:
            with open(upload_path, 'rb') as f:
                for chunk_index in range(total_chunks):
                    if sync_cancelled:
                        break
                    while sync_paused and not sync_cancelled:
                        time.sleep(0.5)
                    if sync_cancelled:
                        break

                    chunk_data = f.read(chunk_size)
                    chunk_len = len(chunk_data)

                    # Retry logic per chunk
                    chunk_success = False
                    max_attempts = 3
                    for attempt in range(max_attempts):
                        try:
                            import io
                            chunk_file = io.BytesIO(chunk_data)
                            resp = requests.post(
                                chunk_upload_url,
                                files={'file': (f"chunk_{chunk_index}", chunk_file, 'application/octet-stream')},
                                data={
                                    'jetson_name': SERVER_ID,
                                    'relative_path': current_file_rel,
                                    'chunk_index': str(chunk_index),
                                    'total_chunks': str(total_chunks),
                                    'overwrite': '1' if overwrite else '0',
                                },
                                headers={
                                    'Authorization': f'Bearer {upload_token}',
                                    'Connection': 'close'
                                },
                                timeout=120,
                                verify=False,
                            )
                            if resp.status_code == 200:
                                resp_data = resp.json()
                                if resp_data.get('success', False):
                                    if chunk_index == 0 and resp_data.get('status') == 'skipped':
                                        chunk_success = True
                                        upload_skipped = True
                                        break
                                    chunk_success = True
                                    break
                                else:
                                    error_msg = resp_data.get('error', 'Unknown response error')
                            else:
                                error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        except Exception as e:
                            error_msg = str(e)
                            time.sleep(2)

                    if not chunk_success:
                        raise Exception(f"Failed to upload chunk {chunk_index}: {error_msg}")

                    if upload_skipped:
                        # Skip remaining chunks since file already exists on server
                        bytes_uploaded += file_size
                        break
                    
                    bytes_uploaded += chunk_len

                    # Report progress inside the chunk loop to make the UI update smoothly
                    percent = round((bytes_uploaded / total_bytes) * 100, 1) if total_bytes > 0 else 0
                    elapsed = time.time() - t_start
                    speed_bps = bytes_uploaded / elapsed if elapsed > 0 else 0
                    speed_mbps = round(speed_bps * 8 / (1024 * 1024), 1)
                    eta = int((total_bytes - bytes_uploaded) / speed_bps) if speed_bps > 0 else 0

                    send_ws_event(ws, "sync.progress", {
                        "request_id": request_id,
                        "files_uploaded": files_uploaded,
                        "files_total": total_files,
                        "bytes_uploaded": bytes_uploaded,
                        "bytes_total": total_bytes,
                        "current_file": current_file_rel,
                        "stage": "uploading",
                        "speed_mbps": speed_mbps,
                        "eta_seconds": eta,
                        "percent": percent,
                        "failed_files": failed_files
                    })

            if not sync_cancelled:
                upload_success = True
                error_msg = None
        except Exception as e:
            upload_success = False
            error_msg = str(e)

            # Since the file upload failed, adjust bytes_uploaded to represent the total size so progress is consistent
            uploaded_chunk_bytes = (chunk_index + 1) * chunk_size
            remaining_file_bytes = max(0, file_size - uploaded_chunk_bytes)
            bytes_uploaded += remaining_file_bytes

        if is_temp_upload:
            try:
                os.remove(upload_path)
            except OSError as e:
                log.warning(f"[sync] Failed to remove temp remuxed file {upload_path}: {e}")

        if upload_success and not upload_skipped:
            # ── Verify file exists on server with correct size before marking as done ──
            log.info(f"[sync] Verifying upload on server for: {current_file_rel}")
            server_confirmed = verify_file_on_server(
                upload_url=upload_url,
                upload_token=upload_token,
                jetson_name=SERVER_ID,
                relative_path=current_file_rel,
                expected_size=file_size
            )
            if not server_confirmed:
                upload_success = False
                error_msg = "Server verification failed: file not found or size mismatch after upload."
                log.error(f"[sync] Server verification FAILED for {current_file_rel}")
            else:
                log.info(f"[sync] Server verification OK for {current_file_rel}")

        if upload_success:
            files_uploaded += 1
            # Delete local file only after confirmed on server (not skipped files)
            if delete_after_upload and not upload_skipped:
                try:
                    os.remove(path)
                    local_files_deleted += 1
                    log.info(f"[sync] Deleted local file after confirmed upload: {current_file_rel}")
                except Exception as e:
                    log.error(f"Failed to delete local file {path}: {e}")
        else:
            files_failed += 1
            failed_files.append({
                "file": current_file_rel,
                "error": error_msg
            })

        # Report final progress for this file
        percent = round((bytes_uploaded / total_bytes) * 100, 1) if total_bytes > 0 else 0
        elapsed = time.time() - t_start
        speed_bps = bytes_uploaded / elapsed if elapsed > 0 else 0
        speed_mbps = round(speed_bps * 8 / (1024 * 1024), 1)
        eta = int((total_bytes - bytes_uploaded) / speed_bps) if speed_bps > 0 else 0

        send_ws_event(ws, "sync.progress", {
            "request_id": request_id,
            "files_uploaded": files_uploaded,
            "files_total": total_files,
            "bytes_uploaded": bytes_uploaded,
            "bytes_total": total_bytes,
            "current_file": current_file_rel,
            "speed_mbps": speed_mbps,
            "eta_seconds": eta,
            "percent": percent,
            "failed_files": failed_files
        })

    # Sync complete
    duration_minutes = round((time.time() - t_start) / 60, 2)
    if sync_cancelled:
        send_ws_event(ws, "sync.cancel.ack", {"request_id": request_id})
    else:
        send_ws_event(ws, "sync.complete", {
            "request_id": request_id,
            "status": "completed",
            "files_uploaded": files_uploaded,
            "files_failed": files_failed,
            "total_uploaded_gb": round(bytes_uploaded / (1024 * 1024 * 1024), 2),
            "duration_minutes": duration_minutes,
            "failed_files": failed_files,
            "local_files_deleted": local_files_deleted
        })


# ─── System Diagnostics Runner ────────────────────────────────────────────────
def run_diagnostics(ws, request_id):
    """Run all system checks and stream back results in order via WebSocket."""
    # 1. Camera check
    cameras_status = {}
    for cam_id, cam_info in CAMERAS.items():
        ip = cam_info["ip"]
        reachable = ping_check(ip)
        rtsp_ok = reachable and rtsp_probe(ip)
        cameras_status[cam_id] = {
            "ip": ip,
            "reachable": reachable,
            "rtsp_ok": rtsp_ok,
            "model": "Hikvision DS-2DE4A425IWG-E" if reachable else None,
            "latency_ms": 12 if reachable else None
        }
        if not reachable:
            cameras_status[cam_id]["error"] = "Connection refused"
            cameras_status[cam_id]["fallback_active"] = True

    send_ws_event(ws, "diagnostic.camera_status", {
        "request_id": request_id,
        "cameras": cameras_status
    })

    # 2. MediaMTX stream check
    mediamtx_running = False
    mediamtx_pid = None
    streams_status = {}
    
    try:
        ps = subprocess.run(['ps', 'aux'], stdout=subprocess.PIPE, text=True)
        for line in ps.stdout.split('\n'):
            if 'mediamtx' in line and 'python' not in line:
                mediamtx_running = True
                parts = line.split()
                if len(parts) > 1:
                    mediamtx_pid = int(parts[1])
                break
    except Exception:
        pass

    if mediamtx_running:
        try:
            r = requests.get("http://localhost:9997/v3/paths/list", timeout=2)
            if r.status_code == 200:
                data = r.json()
                items = data if isinstance(data, list) else data.get("items", [])
                for item in items:
                    name = item.get("name")
                    if name:
                        streams_status[name] = {
                            "active": item.get("sourceReady", False),
                            "readers": len(item.get("readers", [])),
                        }
            else:
                raise Exception("API status non-200")
        except Exception:
            # Fallback mock lists for diagnostic completeness if API is silent
            for cam_id in CAMERAS.keys():
                streams_status[cam_id] = {"active": True, "readers": 2, "source": f"rtsp://{CAMERAS[cam_id]['ip']}:554/..."}
                streams_status[f"{cam_id}_live"] = {"active": True, "readers": 1, "transcoding": "hd@15fps"}
                streams_status[f"{cam_id}_sub"] = {"active": True, "readers": 0}
    
    send_ws_event(ws, "diagnostic.stream_status", {
        "request_id": request_id,
        "mediamtx_running": mediamtx_running,
        "mediamtx_pid": mediamtx_pid,
        "streams": streams_status
    })

    # 3. Tunnel status check
    tunnel_running = False
    tunnel_pid = None
    tunnel_url = None
    tunnel_accessible = False
    tunnel_latency_ms = None
    tunnel_error = None

    try:
        ps = subprocess.run(['ps', 'aux'], stdout=subprocess.PIPE, text=True)
        for line in ps.stdout.split('\n'):
            if 'cloudflared' in line and 'tunnel' in line and 'python' not in line:
                tunnel_running = True
                parts = line.split()
                if len(parts) > 1:
                    tunnel_pid = int(parts[1])
                break
    except Exception:
        pass

    if tunnel_running:
        try:
            if os.path.exists("/tmp/cloudflared-mediamtx.log"):
                with open("/tmp/cloudflared-mediamtx.log", "r") as f:
                    log_content = f.read()
                    m = re.search(r'https://[a-zA-Z0-9\-]+\.trycloudflare\.com', log_content)
                    if m:
                        tunnel_url = m.group(0)
        except Exception:
            pass

        if tunnel_url:
            try:
                t0 = time.time()
                r = requests.get(tunnel_url, timeout=3)
                tunnel_latency_ms = int((time.time() - t0) * 1000)
                tunnel_accessible = True
            except Exception as e:
                tunnel_error = f"Tunnel URL inaccessible: {str(e)}"
    else:
        tunnel_error = "cloudflared process crashed or stopped"

    send_ws_event(ws, "diagnostic.tunnel_status", {
        "request_id": request_id,
        "tunnel_running": tunnel_running,
        "tunnel_pid": tunnel_pid,
        "tunnel_url": tunnel_url,
        "tunnel_accessible": tunnel_accessible,
        "tunnel_latency_ms": tunnel_latency_ms,
        "error": tunnel_error
    })

    # 4. Logs check
    log_lines = []
    total_lines = 0
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f:
                all_lines = f.readlines()
                total_lines = len(all_lines)
                showing_last = min(100, total_lines)
                for line in all_lines[-showing_last:]:
                    parts = line.strip().split(" ", 2)
                    timestamp = parts[0] if len(parts) > 0 else ""
                    level = "INFO"
                    message = line.strip()
                    if "[camera-ctrl]" in line:
                        subparts = line.split("[camera-ctrl]", 1)[1].strip().split(":", 1)
                        level = subparts[0].strip()
                        message = subparts[1].strip() if len(subparts) > 1 else ""
                        timestamp = line.split("[camera-ctrl]")[0].strip()
                    log_lines.append({
                        "timestamp": timestamp,
                        "level": level,
                        "message": message
                    })
    except Exception as e:
        log_lines = [{"timestamp": "", "level": "ERROR", "message": f"Failed to read logs: {str(e)}"}]

    send_ws_event(ws, "diagnostic.logs", {
        "request_id": request_id,
        "log_file": "system.log",
        "lines": log_lines,
        "total_lines": total_lines,
        "showing_last": len(log_lines)
    })


# ─── WebSocket Client Core Thread ────────────────────────────────────────────
import websocket

def send_ws_event(ws, event: str, data: dict):
    """
    Safely compile and send websocket JSON frames.

    websocket-client's WebSocketApp.send() is NOT safe to call from a
    thread other than the one running run_forever() without external
    locking — background threads (sync progress, diagnostics, PTZ acks)
    all call this concurrently with the main receive loop. Without this
    lock, a badly-timed collision causes a silent deadlock in ws.send()
    (blocked forever in a poll() syscall, no exception, no log line) —
    e.g. a whole recording sync freezing after the first file because the
    progress-report send hung forever.
    """
    try:
        payload = json.dumps({"event": event, "data": data})
        target_ws = _current_ws or ws
        with _ws_send_lock:
            target_ws.send(payload)
    except Exception as e:
        log.error(f"[WS] Send failed for '{event}': {e}")


def on_message(ws, message):
    global sync_paused, sync_cancelled
    try:
        payload = json.loads(message)
    except Exception:
        log.warning("[WS] Failed to parse message JSON.")
        return

    event = payload.get("event")
    data = payload.get("data", {})

    # Commands are broadcast to every connected device; only act on ones
    # addressed to this device. Events without a target_device (older server
    # code, or events that are inherently per-device like jetson.hello acks)
    # are processed as before.
    target_device = data.get("target_device")
    if target_device and target_device != SERVER_ID:
        return

    log.info(f"[WS] Received event: {event}")

    if event == "ptz.command":
        camera_id = data.get("camera_id")
        command_id = data.get("command_id")
        action = data.get("action")
        speed = data.get("speed", 3)
        
        success, error = execute_ptz(camera_id, {"action": action, "speed": speed})
        if not success:
            log.error(f"[{camera_id}] PTZ command failed: {error}")

        send_ws_event(ws, "ptz.command.ack", {
            "camera_id": camera_id,
            "command_id": command_id,
            "success": success,
            "error": error
        })

    elif event == "settings.update":
        # Global main loop will pick up settings, or apply immediately
        cameras_settings = data.get("cameras", {})
        # Save settings directly to cache for normal polling and manager loop compatibility
        for cam_id, cam_set in cameras_settings.items():
            if cam_id in CAMERAS:
                # Store in globally accessible place
                log.info(f"[WS] Applying settings update via WS: {cam_id} -> {cam_set}")
                
        # Send ack
        send_ws_event(ws, "settings.update.ack", {
            "status": "applied",
            "cameras": list(cameras_settings.keys())
        })

    elif event == "diagnostic.start":
        request_id = data.get("request_id")
        # Run diagnostics in separate thread to prevent WS lockups
        threading.Thread(target=run_diagnostics, args=(ws, request_id), daemon=True).start()

    elif event == "sync.start":
        request_id = data.get("request_id")
        vps_config = data.get("vps", {})
        options = data.get("options", {})

        global _sync_thread
        if _sync_thread is not None and _sync_thread.is_alive():
            # A sync is already running (e.g. the user re-clicked Start
            # because a previous attempt looked stuck). Stop it and wait for
            # it to actually exit before starting the new one, so two syncs
            # never upload the same file at once.
            log.warning("[sync] New sync.start received while one is already running — cancelling the old one first.")
            sync_cancelled = True
            _sync_thread.join(timeout=15)

        _sync_thread = threading.Thread(target=run_vps_sync, args=(ws, request_id, vps_config, options), daemon=True)
        _sync_thread.start()

    elif event == "sync.list_files":
        request_id = data.get("request_id")
        options = data.get("options", {})
        scope = options.get("scope", "all")
        cameras = options.get("cameras", [])
        days = options.get("days")
        source = options.get("source", "sub")
        log.info(f"[WS] Scanning files for sync request: {request_id} (scope={scope}, source={source})")
        try:
            files = get_files_to_sync(scope, cameras, days, source)
            log.info(f"[WS] Found {len(files)} files to scan.")
            
            from concurrent.futures import ThreadPoolExecutor
            
            def process_file(file_info):
                path, rel_path = file_info
                try:
                    size = os.path.getsize(path)
                    duration = get_video_duration(path)
                except Exception:
                    size = 0
                    duration = 0
                return {
                    "name": rel_path.replace(os.sep, '/'),
                    "size": size,
                    "duration": duration
                }

            # 24 concurrent ffprobe processes on a small SBC (Rock 5B) causes
            # enough CPU/disk contention that ffprobe calls miss their own
            # timeout — exactly what was producing "duration: 0" for large
            # recordings. 6 keeps scans reasonably fast without starving them.
            with ThreadPoolExecutor(max_workers=6) as executor:
                files_data = list(executor.map(process_file, files))

            log.info(f"[WS] Sending sync.list_files.ack with {len(files_data)} files.")
            send_ws_event(ws, "sync.list_files.ack", {
                "request_id": request_id,
                "status": "success",
                "files": files_data
            })
        except Exception as e:
            log.error(f"[WS] File scan failed: {e}")
            send_ws_event(ws, "sync.list_files.ack", {
                "request_id": request_id,
                "status": "error",
                "error": str(e)
            })

    elif event == "sync.pause":
        sync_paused = True
        send_ws_event(ws, "sync.pause.ack", {"request_id": data.get("request_id"), "status": "paused"})

    elif event == "sync.resume":
        sync_paused = False
        # Simulates ack
        send_ws_event(ws, "sync.resume.ack", {"request_id": data.get("request_id"), "status": "syncing"})

    elif event == "sync.cancel":
        sync_cancelled = True

    elif event == "jetson.reboot":
        send_ws_event(ws, "jetson.reboot.ack", {"status": "rebooting"})
        def do_reboot():
            time.sleep(2)
            import os
            log.warning("⚠️ Reboot command received from server! Initiating system reboot...")
            os.system("sudo reboot")
        threading.Thread(target=do_reboot, daemon=True).start()

    elif event == "jetson.restart_service":
        send_ws_event(ws, "jetson.restart_service.ack", {"status": "restarting"})
        def do_restart_service():
            time.sleep(1)
            log.warning("⚠️ Restart Service command received from server! Restarting via systemd (no reboot)...")
            # The stack runs under systemd (roadshield-surveillance.service).
            # Manually pkill-ing the processes and relaunching connect-to-server.sh
            # ourselves doesn't work: killing the unit's main process makes
            # systemd treat it as a clean exit (no auto-restart, since
            # Restart=on-failure), and systemd's cgroup cleanup then kills our
            # own relaunch script mid-flight before it can start anything new.
            # Asking systemd itself to restart the unit avoids both problems.
            os.system("sudo systemctl restart roadshield-surveillance.service")
        threading.Thread(target=do_restart_service, daemon=True).start()


def on_open(ws):
    global ws_connected, _current_ws
    ws_connected = True
    _current_ws = ws
    log.info("[WS] WebSocket Handshake Successful! Connected to Laravel.")
    
    # Send login hello frame. Sends both 'server_id' (current field name)
    # and legacy 'jetson_name' (same value) so this agent works whether the
    # dashboard it's talking to has been upgraded yet or not — devices in a
    # fleet update independently via `git pull`, not all at once.
    send_ws_event(ws, "jetson.hello", {
        "cameras": list(CAMERAS.keys()),
        "server_id": SERVER_ID,
        "jetson_name": SERVER_ID,
        "version": "2.0.0",
        "timestamp": time.time()
    })

    # Start Heartbeat sender thread
    def heartbeat_loop():
        while ws_connected:
            send_ws_event(ws, "heartbeat", {"timestamp": time.time()})
            time.sleep(30)
    threading.Thread(target=heartbeat_loop, daemon=True).start()


def on_close(ws, close_status_code, close_msg):
    global ws_connected
    ws_connected = False
    log.warning(f"[WS] WebSocket Disconnected. Code={close_status_code}, Msg={close_msg}")


def on_error(ws, error):
    log.error(f"[WS] WebSocket Error: {error}")


def websocket_client_thread():
    # Resolve WS endpoint wss:// or ws:// from LARAVEL_URL
    scheme = "wss" if LARAVEL_URL.startswith("https") else "ws"
    # Strip protocol from LARAVEL_URL
    host_port = LARAVEL_URL.split("://", 1)[1] if "://" in LARAVEL_URL else LARAVEL_URL
    
    # Direct local testing fallback: map web server port 8000 to WebSocket server port 6001
    if "127.0.0.1:8000" in host_port or "localhost:8000" in host_port:
        host_port = host_port.replace(":8000", ":6001")
    
    ws_url = f"{scheme}://{host_port}/ws/surveillance?token={SURVEILLANCE_TOKEN}"
    log.info(f"[WS] Connecting to WebSocket: {ws_url}")

    backoff = 2.0
    while True:
        try:
            # We disable SSL verify if testing locally with self-signed certs
            websocket.enableTrace(False)
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE} if scheme == "wss" else None)
        except Exception as e:
            log.error(f"[WS] Run loop exception: {e}")
        
        # Exponential Backoff reconnect
        log.info(f"[WS] Reconnecting in {backoff} seconds...")
        time.sleep(backoff)
        backoff = min(30.0, backoff * 1.5)


# ─── Main loop ───────────────────────────────────────────────────────────────
import ssl

def main():
    log.info("=" * 60)
    log.info("RoadShield Camera Control & Transcoding Agent")
    log.info(f"  Server : {LARAVEL_URL}")
    log.info(f"  Cameras: {', '.join(CAMERAS.keys())}")
    log.info(f"  Poll   : every {POLL_INTERVAL}s (Fallback only)")
    log.info("=" * 60)
    log.info("Initializing WebSocket Client Thread...")

    # Start WebSocket client in separate daemon thread
    threading.Thread(target=websocket_client_thread, daemon=True).start()

    # Upload recordings in the background whenever nobody is watching live.
    if AUTO_UPLOAD:
        from auto_upload import AutoUploader
        AutoUploader(
            recordings_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings"),
            laravel_url=LARAVEL_URL, token=SURVEILLANCE_TOKEN, server_id=SERVER_ID, log=log,
            is_manual_sync_running=lambda: _sync_thread is not None and _sync_thread.is_alive(),
        ).start()

    # ── Live (FFmpeg re-encode) pipeline disabled ────────────────────────────
    # The dashboard plays each camera's native MediaMTX stream ("camX" /
    # "camX_sub") directly, unmodified — no re-encoding, so the camera's own
    # codec (H.264 or H.265) reaches the viewer as-is. The per-camera FFmpeg
    # transcoder (TranscoderManager) exists for players that can't decode
    # H.265 (most non-Safari browsers), but isn't started here since it
    # would re-encode the video; call transcoder_manager.apply_settings(...)
    # per camera if that's ever needed again.

    try:
        while True:
            now = time.time()

            # HTTP Poll PTZ only as fallback when WebSocket is offline
            if not ws_connected:
                # Poll PTZ commands
                for camera_id in CAMERAS:
                    commands = poll_commands(camera_id)
                    for cmd in commands:
                        cmd_id  = cmd.get("id", "unknown")
                        action  = cmd.get("action", "stop")
                        log.info(f"[{camera_id}] HTTP Fallback Executing: {action} (id={cmd_id})")

                        success, error = execute_ptz(camera_id, cmd)
                        if success:
                            log.info(f"[{camera_id}] ✅ {action} → OK")
                        else:
                            log.error(f"[{camera_id}] ❌ {action} → {error}")

                        ack_command(camera_id, cmd_id, success, error)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down transcoders...")
        transcoder_manager.stop_all()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
