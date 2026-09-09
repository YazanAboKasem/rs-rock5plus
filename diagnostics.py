#!/usr/bin/env python3
"""
RoadShield — diagnostics.py
==============================
Diagnostic handlers for Test Mode. Checks camera connectivity, stream health,
Cloudflare tunnel status, and provides log viewing — all triggered via WebSocket
events from the Laravel dashboard.

Events handled:
  diagnostic.start → runs all requested checks and sends results back
"""

import os
import time
import subprocess
import logging
import json
import re
from typing import Optional
from requests.auth import HTTPDigestAuth

import requests

log = logging.getLogger("diagnostics")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "system.log")
CAMERA_LOG_FILE = os.path.join(SCRIPT_DIR, "system.log.camera")
MEDIAMTX_API = "http://127.0.0.1:9997"
TUNNEL_LOG = "/tmp/cloudflared-mediamtx.log"


# ─── Camera Connectivity Check ──────────────────────────────────────────────

def check_camera(camera_id: str, cam_config: dict, transcoder_manager=None) -> dict:
    """
    Check if a camera is reachable and its RTSP stream is working.

    Returns a status dict for this camera.
    """
    ip = cam_config["ip"]
    user = cam_config["user"]
    password = cam_config["password"]
    channel = cam_config.get("channel", 1)

    fallback_active = False
    if transcoder_manager:
        with transcoder_manager.lock:
            mode = transcoder_manager.current_modes.get(camera_id)
            fallback_active = (mode == "fallback")

    result = {
        "ip": ip,
        "reachable": False,
        "rtsp_ok": False,
        "model": None,
        "latency_ms": None,
        "error": None,
        "fallback_active": fallback_active,
    }

    # 1. Ping check
    try:
        start = time.time()
        ping_result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ip],
            capture_output=True, text=True, timeout=5
        )
        latency = round((time.time() - start) * 1000)

        if ping_result.returncode == 0:
            result["reachable"] = True
            result["latency_ms"] = latency
        else:
            result["error"] = f"Ping failed: host unreachable"
            return result
    except subprocess.TimeoutExpired:
        result["error"] = "Ping timeout (5s)"
        return result
    except Exception as e:
        result["error"] = f"Ping error: {e}"
        return result

    # 2. ISAPI device info (get model name)
    try:
        auth = HTTPDigestAuth(user, password)
        r = requests.get(
            f"http://{ip}/ISAPI/System/deviceInfo",
            auth=auth, timeout=3
        )
        if r.status_code == 200:
            # Parse XML for model
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.text)
            ns = {"ns": "http://www.hikvision.com/ver20/XMLSchema"}
            model_elem = root.find(".//ns:model", ns)
            if model_elem is None:
                # Try without namespace
                model_elem = root.find(".//model")
            if model_elem is not None:
                result["model"] = model_elem.text
    except Exception:
        pass  # Model info is optional

    # 3. RTSP probe — try to connect to the camera's RTSP stream
    try:
        rtsp_url = f"rtsp://{user}:{password}@{ip}:554/Streaming/Channels/{channel}01"
        probe = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                "-show_entries", "stream=codec_name,width,height",
                "-of", "json",
                "-timeout", "3000000",  # 3 seconds in microseconds
            ],
            capture_output=True, text=True, timeout=8
        )
        if probe.returncode == 0:
            result["rtsp_ok"] = True
        else:
            result["rtsp_ok"] = False
            result["error"] = "RTSP stream not available"
    except subprocess.TimeoutExpired:
        result["rtsp_ok"] = False
        result["error"] = "RTSP probe timeout"
    except FileNotFoundError:
        # ffprobe not installed, skip RTSP check
        result["rtsp_ok"] = None  # unknown
        log.warning("ffprobe not found — skipping RTSP check")
    except Exception as e:
        result["rtsp_ok"] = False
        result["error"] = f"RTSP probe error: {e}"

    return result


def check_all_cameras(cameras: dict, transcoder_manager=None) -> dict:
    """Check all cameras and return status dict."""
    results = {}
    for camera_id, cam_config in cameras.items():
        log.info(f"[diag] Checking camera: {camera_id} ({cam_config['ip']})")
        results[camera_id] = check_camera(camera_id, cam_config, transcoder_manager=transcoder_manager)

        status = "✅ OK" if results[camera_id]["reachable"] else "❌ FAIL"
        log.info(f"[diag] {camera_id}: {status}")

    return results


# ─── Stream Health Check (MediaMTX API) ─────────────────────────────────────

def check_streams() -> dict:
    """
    Query MediaMTX API to get status of all active streams.
    MediaMTX API: http://localhost:9997/v3/paths/list
    """
    result = {
        "mediamtx_running": False,
        "mediamtx_pid": None,
        "streams": {},
    }

    # Check if MediaMTX process is running
    try:
        ps_result = subprocess.run(
            ["pgrep", "-f", "mediamtx"],
            capture_output=True, text=True, timeout=3
        )
        if ps_result.returncode == 0:
            pids = ps_result.stdout.strip().split("\n")
            result["mediamtx_running"] = True
            result["mediamtx_pid"] = int(pids[0])
    except Exception:
        pass

    if not result["mediamtx_running"]:
        return result

    # Query MediaMTX API for stream paths
    try:
        r = requests.get(f"{MEDIAMTX_API}/v3/paths/list", timeout=3)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", [])
            for item in items:
                name = item.get("name", "unknown")
                source = item.get("source", {})
                readers = item.get("readers", [])

                stream_info = {
                    "active": item.get("ready", False),
                    "readers": len(readers) if isinstance(readers, list) else 0,
                    "source": None,
                    "error": None,
                }

                if source:
                    source_type = source.get("type", "")
                    source_id = source.get("id", "")
                    stream_info["source"] = f"{source_type}: {source_id}" if source_type else None

                if not stream_info["active"]:
                    stream_info["error"] = "source not found or not ready"

                result["streams"][name] = stream_info
    except requests.exceptions.ConnectionError:
        log.warning("[diag] Cannot connect to MediaMTX API — is it running?")
    except Exception as e:
        log.warning(f"[diag] MediaMTX API error: {e}")

    return result


# ─── Cloudflare Tunnel Status ────────────────────────────────────────────────

def check_tunnel() -> dict:
    """Check Cloudflare tunnel process and URL."""
    result = {
        "tunnel_running": False,
        "tunnel_pid": None,
        "tunnel_url": None,
        "tunnel_accessible": False,
        "tunnel_latency_ms": None,
        "error": None,
    }

    # Check if cloudflared is running
    try:
        ps_result = subprocess.run(
            ["pgrep", "-f", "cloudflared tunnel"],
            capture_output=True, text=True, timeout=3
        )
        if ps_result.returncode == 0:
            pids = ps_result.stdout.strip().split("\n")
            result["tunnel_running"] = True
            result["tunnel_pid"] = int(pids[0])
    except Exception:
        pass

    if not result["tunnel_running"]:
        result["error"] = "cloudflared process not running"
        return result

    # Extract tunnel URL from log
    tunnel_url = None
    if os.path.isfile(TUNNEL_LOG):
        try:
            with open(TUNNEL_LOG, "r") as f:
                content = f.read()

            # Try plain text match
            match = re.search(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com", content)
            if match:
                tunnel_url = match.group(0)
            else:
                # Try JSON log format
                for line in content.split("\n"):
                    try:
                        msg = json.loads(line).get("message", "")
                        m = re.search(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com", msg)
                        if m:
                            tunnel_url = m.group(0)
                            break
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except Exception as e:
            log.warning(f"[diag] Error reading tunnel log: {e}")

    if tunnel_url:
        result["tunnel_url"] = tunnel_url

        # Check if the tunnel URL is accessible
        try:
            start = time.time()
            r = requests.head(tunnel_url, timeout=10, allow_redirects=True)
            latency = round((time.time() - start) * 1000)
            result["tunnel_accessible"] = r.status_code < 500
            result["tunnel_latency_ms"] = latency
        except Exception as e:
            result["tunnel_accessible"] = False
            result["error"] = f"Tunnel URL not accessible: {e}"
    else:
        result["error"] = "Tunnel URL not found in logs"

    return result


# ─── Log Viewer ──────────────────────────────────────────────────────────────

def read_logs(log_file: str = None, last_n: int = 100) -> dict:
    """
    Read the last N lines from the system log file.
    Returns structured log entries.
    """
    if log_file is None:
        log_file = LOG_FILE

    result = {
        "log_file": os.path.basename(log_file),
        "lines": [],
        "total_lines": 0,
        "showing_last": last_n,
    }

    if not os.path.isfile(log_file):
        result["lines"] = [{"timestamp": "", "level": "WARN", "message": f"Log file not found: {log_file}"}]
        return result

    try:
        # Count total lines efficiently
        wc_result = subprocess.run(
            ["wc", "-l", log_file],
            capture_output=True, text=True, timeout=5
        )
        if wc_result.returncode == 0:
            result["total_lines"] = int(wc_result.stdout.strip().split()[0])

        # Read last N lines
        tail_result = subprocess.run(
            ["tail", "-n", str(last_n), log_file],
            capture_output=True, text=True, timeout=5
        )
        if tail_result.returncode == 0:
            for line in tail_result.stdout.strip().split("\n"):
                if not line.strip():
                    continue

                entry = parse_log_line(line)
                result["lines"].append(entry)

    except Exception as e:
        result["lines"] = [{"timestamp": "", "level": "ERROR", "message": f"Error reading log: {e}"}]

    return result


def parse_log_line(line: str) -> dict:
    """Parse a log line into structured format."""
    # Try format: [2026-07-12 12:30:00] [LEVEL] message
    match = re.match(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]\s+\[(\w+)\]\s+(.*)", line)
    if match:
        return {
            "timestamp": match.group(1),
            "level": match.group(2),
            "message": match.group(3),
        }

    # Try format: HH:MM:SS [module] LEVEL: message
    match = re.match(r"(\d{2}:\d{2}:\d{2})\s+\[[\w-]+\]\s+(\w+):\s+(.*)", line)
    if match:
        return {
            "timestamp": match.group(1),
            "level": match.group(2),
            "message": match.group(3),
        }

    # Fallback — raw line
    return {
        "timestamp": "",
        "level": "INFO",
        "message": line.strip(),
    }


# ─── Master Diagnostic Handler ──────────────────────────────────────────────

def handle_diagnostic(data: dict, cameras: dict, ws_client=None, transcoder_manager=None) -> dict:
    """
    Handle diagnostic.start event. Runs requested checks and sends results
    back via WebSocket as separate events.

    Expected data:
        {
            "checks": ["cameras", "streams", "tunnel", "logs"],
            "request_id": "diag-abc123",
            "log_lines": 100
        }
    """
    request_id = data.get("request_id", "unknown")
    checks = data.get("checks", ["cameras", "streams", "tunnel", "logs"])
    log_lines = data.get("log_lines", 100)

    log.info(f"[diag] Starting diagnostics (id={request_id}, checks={checks})")

    results = {}

    # Camera connectivity
    if "cameras" in checks:
        log.info("[diag] Checking cameras...")
        camera_status = check_all_cameras(cameras, transcoder_manager=transcoder_manager)
        results["cameras"] = camera_status

        if ws_client:
            ws_client.send_sync("diagnostic.camera_status", {
                "request_id": request_id,
                "cameras": camera_status,
            })

    # Stream health
    if "streams" in checks:
        log.info("[diag] Checking streams...")
        stream_status = check_streams()
        results["streams"] = stream_status

        if ws_client:
            ws_client.send_sync("diagnostic.stream_status", {
                "request_id": request_id,
                **stream_status,
            })

    # Cloudflare tunnel
    if "tunnel" in checks:
        log.info("[diag] Checking tunnel...")
        tunnel_status = check_tunnel()
        results["tunnel"] = tunnel_status

        if ws_client:
            ws_client.send_sync("diagnostic.tunnel_status", {
                "request_id": request_id,
                **tunnel_status,
            })

    # Logs
    if "logs" in checks:
        log.info("[diag] Reading logs...")
        # Read both system log and camera log
        system_logs = read_logs(LOG_FILE, last_n=log_lines)
        results["logs"] = system_logs

        if ws_client:
            ws_client.send_sync("diagnostic.logs", {
                "request_id": request_id,
                **system_logs,
            })

    log.info(f"[diag] Diagnostics complete (id={request_id})")

    return {
        "request_id": request_id,
        "status": "completed",
        "checks_run": checks,
    }
