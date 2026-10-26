#!/usr/bin/env python3
"""
RoadShield — sync_camera_config.py
================================
Pulls this device's camera list (IP/credentials/channel/type) from the
Laravel dashboard and:
  1. Writes cameras_config.json — loaded by camera-control.py instead of
     its old hardcoded CAMERAS dict.
  2. Regenerates the `paths:` section of mediamtx.yml so MediaMTX pulls
     the right RTSP sources.

This is what lets a camera be registered entirely from the dashboard
(IP, username, password, PTZ) instead of hand-editing files on every
device. Run before MediaMTX starts (see connect-to-server.sh).

Safety: if the device isn't registered yet, or has no cameras
configured, this is a no-op — it does NOT touch mediamtx.yml. Only a
real, non-empty camera list ever triggers a rewrite.
"""

import json
import os
import re
import sys
from urllib.parse import quote, urlparse

import requests

from device_identity import get_device_id

LARAVEL_URL = "https://controlroom.roadshield.ae"
SURVEILLANCE_TOKEN = "b8e2ed9ae5def597e6a59f2801fca19fa758ab1a0cd3e9900b708b3aa357bc3c"
SERVER_ID = get_device_id()

for arg in sys.argv:
    if arg.startswith("--url="):
        LARAVEL_URL = arg.split("=", 1)[1]
    elif arg.startswith("--token="):
        SURVEILLANCE_TOKEN = arg.split("=", 1)[1]
    elif arg.startswith("--server-name=") or arg.startswith("--jetson-name="):
        SERVER_ID = arg.split("=", 1)[1]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIAMTX_CONFIG = os.path.join(SCRIPT_DIR, "mediamtx.yml")
CAMERAS_CONFIG_JSON = os.path.join(SCRIPT_DIR, "cameras_config.json")
EVENT_TOKEN_JSON = os.path.join(SCRIPT_DIR, "event_token.json")
PATHS_MARKER = "paths:"


def log(msg):
    print(f"[sync-camera-config] {msg}")


def fetch_camera_config():
    url = f"{LARAVEL_URL}/api/surveillance/devices/{SERVER_ID}/camera-config"
    headers = {
        "Authorization": f"Bearer {SURVEILLANCE_TOKEN}",
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            log(f"Fetch failed: HTTP {r.status_code} — {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        log(f"Fetch failed: {e}")
        return None


def rtsp_source(cam: dict, sub: bool) -> str:
    # An explicit URL from the dashboard always wins — most reliable, since
    # the user already knows it works. Injects credentials if the URL
    # doesn't already have an "://user:pass@" section.
    explicit = (cam.get("rtsp_sub_url") if sub else cam.get("rtsp_main_url")) or ""
    explicit = explicit.strip()
    if explicit:
        user = cam.get("username") or ""
        pw = cam.get("password") or ""
        if (user or pw) and "@" not in explicit.split("://", 1)[-1].split("/", 1)[0]:
            scheme, rest = explicit.split("://", 1)
            explicit = f"{scheme}://{quote(user, safe='')}:{quote(pw, safe='')}@{rest}"
        return explicit

    # No explicit URL — fall back to composing one from ip/type/channel/port.
    user = quote(cam.get("username") or "", safe="")
    pw = quote(cam.get("password") or "", safe="")
    ip = cam.get("ip") or ""
    port = cam.get("rtsp_port") or 554
    channel = cam.get("channel") or 1
    auth = f"{user}:{pw}@" if user or pw else ""

    if cam.get("type") == "generic":
        path = "1" if sub else "0"
        return f"rtsp://{auth}{ip}:{port}/{path}"

    # Hikvision-style: channel N main stream = N01, sub stream = N02
    suffix = f"{channel}02" if sub else f"{channel}01"
    return f"rtsp://{auth}{ip}:{port}/Streaming/Channels/{suffix}"


def sanitize_path_key(raw_key: str) -> str:
    """
    MediaMTX path names may only contain alphanumeric characters, underscore,
    dot, tilde, minus or slash. The dashboard's camera_key is meant to be a
    machine-safe slug (e.g. "cam1"), but it's operator-entered data — a stray
    space or symbol (e.g. "h 4k") would otherwise be written straight into
    mediamtx.yml and crash MediaMTX (and the whole device, since it restarts
    in a loop) on every boot until fixed on the dashboard.
    """
    key = re.sub(r"[^A-Za-z0-9_.~/-]", "_", raw_key.strip())
    key = key.strip("_") or "cam"
    return key


def build_paths_block(cameras: list) -> str:
    lines = [PATHS_MARKER]
    for cam in cameras:
        key = cam["camera_key"]
        label = cam.get("label") or key
        lines.append(f"  # ── {label} ({cam.get('ip', 'no ip')}) — synced from dashboard ──")
        lines.append(f"  {key}:")
        lines.append(f"    source: {rtsp_source(cam, sub=False)}")
        lines.append("    sourceProtocol: tcp")
        lines.append("    record: yes")
        lines.append("")
        lines.append(f"  {key}_sub:")
        lines.append(f"    source: {rtsp_source(cam, sub=True)}")
        lines.append("    sourceProtocol: tcp")
        lines.append("    record: yes")
        lines.append("")
        # Publish-only path (no "source:") for camera-control.py's FFmpeg
        # transcoder to push its browser-compatible H.264 re-encode to —
        # MediaMTX rejects RTSP publishing to any path not already declared
        # in this file, so without this entry the transcoder's ANNOUNCE
        # fails with "400 Bad Request" and no "{key}_live" stream ever exists.
        lines.append(f"  {key}_live:")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def rewrite_mediamtx_yml(cameras: list) -> bool:
    if not os.path.exists(MEDIAMTX_CONFIG):
        log(f"{MEDIAMTX_CONFIG} not found — skipping mediamtx.yml update.")
        return False

    with open(MEDIAMTX_CONFIG) as f:
        current = f.read()

    marker_idx = current.find(f"\n{PATHS_MARKER}")
    if marker_idx == -1:
        log("Could not find 'paths:' section in mediamtx.yml — refusing to touch it.")
        return False

    header = current[: marker_idx + 1]  # everything before "paths:", keep the newline
    new_content = header + build_paths_block(cameras)

    # Keep a backup of the previous version, just in case.
    try:
        with open(MEDIAMTX_CONFIG + ".bak", "w") as f:
            f.write(current)
    except Exception as e:
        log(f"Warning: could not write mediamtx.yml.bak: {e}")

    with open(MEDIAMTX_CONFIG, "w") as f:
        f.write(new_content)

    log(f"mediamtx.yml regenerated with {len(cameras)} camera(s).")
    return True


def _parsed_urls(cam: dict):
    for url in (cam.get("rtsp_main_url"), cam.get("rtsp_sub_url")):
        if url:
            yield urlparse(url)


def resolve_ip(cam: dict) -> str | None:
    """
    The dashboard sometimes leaves a camera's "ip"/"username"/"password"
    fields empty and only supplies a ready-made rtsp_main_url/rtsp_sub_url
    (the "explicit URL always wins" case in rtsp_source()). MediaMTX still
    streams fine in that case since it uses those URLs directly — but
    camera-control.py's PTZ, diagnostics and reachability checks all key off
    cameras_config.json's plain fields, so leaving them null there made a
    camera that was actually streaming show up as "OFFLINE" / "Fallback
    Active" on the dashboard's own test-mode diagnostic, and would have made
    PTZ commands fail authentication too. Fall back to parsing them out of
    either URL.
    """
    if cam.get("ip"):
        return cam["ip"]
    for parsed in _parsed_urls(cam):
        if parsed.hostname:
            return parsed.hostname
    return None


def resolve_credential(cam: dict, field: str) -> str | None:
    key = "username" if field == "user" else "password"
    if cam.get(key):
        return cam[key]
    for parsed in _parsed_urls(cam):
        value = parsed.username if field == "user" else parsed.password
        if value:
            return value
    return None


def write_cameras_config_json(cameras: list):
    data = {}
    for cam in cameras:
        data[cam["camera_key"]] = {
            "ip": resolve_ip(cam),
            "user": resolve_credential(cam, "user"),
            "password": resolve_credential(cam, "password"),
            "channel": cam.get("channel", 1),
            "type": cam.get("type", "hikvision"),
            "rtsp_port": cam.get("rtsp_port", 554),
            "ptz": bool(cam.get("ptz")),
            "label": cam.get("label"),
            "role": cam.get("role"),  # FRONT | REAR_FIXED | REAR_PTZ | None
        }

    with open(CAMERAS_CONFIG_JSON, "w") as f:
        json.dump(data, f, indent=2)

    log(f"cameras_config.json written ({len(data)} camera(s)).")


def write_event_token_json(token):
    # Dedicated per-device credential for POST /api/surveillance/events —
    # kept separate from cameras_config.json since it's not camera data.
    if not token:
        return
    with open(EVENT_TOKEN_JSON, "w") as f:
        json.dump({"event_api_token": token}, f, indent=2)
    log("event_token.json written.")


def main():
    log(f"Fetching camera config for device '{SERVER_ID}' from {LARAVEL_URL}...")
    result = fetch_camera_config()

    if result is None:
        log("Could not reach the server — leaving local config untouched.")
        return

    if not result.get("registered"):
        log(f"Device '{SERVER_ID}' is not registered on the dashboard yet — nothing to sync. "
            f"Register it under Devices > Discovered Devices to enable this.")
        return

    cameras = result.get("cameras", [])
    if not cameras:
        log("Device is registered but has no cameras configured yet — leaving local config untouched.")
        return

    valid_cameras = []
    for cam in cameras:
        raw_key = cam.get("camera_key") or ""
        if not raw_key:
            log(f"Skipping a camera entry with no camera_key: {cam.get('label')}")
            continue
        if not (cam.get("ip") or cam.get("rtsp_main_url")):
            log(f"Skipping camera '{raw_key}' ({cam.get('label')}) — no ip or rtsp_main_url configured on the dashboard yet.")
            continue

        sanitized_key = sanitize_path_key(raw_key)
        if sanitized_key != raw_key:
            log(f"Camera key '{raw_key}' isn't a valid MediaMTX path name — using '{sanitized_key}' instead.")
        cam["camera_key"] = sanitized_key
        valid_cameras.append(cam)

    if not valid_cameras:
        log("No cameras with usable connection info — leaving local config untouched.")
        return

    write_cameras_config_json(valid_cameras)
    rewrite_mediamtx_yml(valid_cameras)
    write_event_token_json(result.get("event_api_token"))


if __name__ == "__main__":
    main()
