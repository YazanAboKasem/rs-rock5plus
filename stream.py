#!/usr/bin/env python3
"""
RoadShield — Smart Surveillance Streaming Service
Phase 1: Read camera → encode H.264 → push RTSP to MediaMTX

IMPORTANT:
  For Hikvision IP cameras, mediamtx.yml already pulls RTSP directly.
  This script is only needed for USB/webcam cameras OR if you want
  GStreamer-based transcoding instead of MediaMTX's built-in pull.

  If mediamtx.yml has 'source:' entries for your cameras, you do NOT
  need to run this script — MediaMTX handles it automatically.

HOW TO CHANGE SERVER FOR DEPLOYMENT:
  Change MEDIA_SERVER below to the Jetson local IP, public server IP,
  or domain name. Nothing else needs to change.
"""

import sys
import os
import time
import signal
import subprocess
import platform
import threading

# =============================================================================
# ▼  SINGLE CONFIG POINT — Change this to deploy to a different server  ▼
# =============================================================================

MEDIA_SERVER = "127.0.0.1"          # Local dev / Jetson / public IP / domain
RTSP_PORT    = 8554                  # MediaMTX RTSP port
SHOW_LOCAL_WINDOWS = True            # Set to True to open a local window for each camera feed

# =============================================================================
# Camera Registry — add cameras here
# =============================================================================

CAMERAS = [
    {
        "id": "cam1",
        "label": "Camera 1 — Front View",

        "source_rtsp": "rtspsrc location=rtsp://admin:hikvision%4012@192.168.1.65:554/Streaming/Channels/101 latency=100",

        "width": 1280,
        "height": 720,
        "fps": 15,
        "bitrate": 2000,
    },
    {
        "id": "cam2",
        "label": "Camera 2 — Rear PTZ",

        "source_rtsp": "rtspsrc location=rtsp://admin:hikvision%4012@192.168.1.64:554/Streaming/Channels/101 latency=100",

        "width": 1280,
        "height": 720,
        "fps": 15,
        "bitrate": 2000,
    },
    {
        "id": "cam3",
        "label": "Camera 3",

        "source_rtsp": "rtspsrc location=rtsp://admin:hikvision%4012@192.168.1.67:554/Streaming/Channels/101 latency=100",

        "width": 1280,
        "height": 720,
        "fps": 15,
        "bitrate": 2000,
    },
    {
        "id": "cam3",
        "label": "Camera 3 — Back View",
        # For USB/webcam (third camera):
        "source_linux": "v4l2src device=/dev/video4",
        "source_macos": "avfvideosrc device-index=2",
        # For IP camera (uncomment to use instead of USB):
        # "source_rtsp": "rtspsrc location=rtsp://admin:hikvision%4012@192.168.1.67:554/Streaming/Channels/101 latency=100",
        "width": 1280,
        "height": 720,
        "fps": 15,
        "bitrate": 2000,
    },
]

# =============================================================================
# Internals
# =============================================================================

RECONNECT_DELAY = 5   # seconds between reconnect attempts

_shutdown = False


def detect_camera_source(cam: dict) -> str:
    """Return the appropriate GStreamer camera source element for this OS."""
    # Prefer RTSP source if configured
    if cam.get("source_rtsp"):
        return cam["source_rtsp"]

    system = platform.system()
    if system == "Linux":
        return cam.get("source_linux", "v4l2src device=/dev/video0")
    elif system == "Darwin":
        return cam.get("source_macos", "avfvideosrc device-index=0")
    else:
        raise RuntimeError(f"Unsupported OS: {system}. Use Linux or macOS.")


def build_gstreamer_pipeline(cam: dict, camera_source: str) -> str:
    """
    Build the GStreamer pipeline string.

    Pipeline:
      camera → videoconvert → videoscale → videorate → x264enc → rtp → rtsp publish

    MediaMTX receives the RTSP stream and re-publishes it as:
      - RTSP  → rtsp://MEDIA_SERVER:8554/{cam_id}
      - HLS   → http://MEDIA_SERVER:8888/{cam_id}/index.m3u8
      - WebRTC → http://MEDIA_SERVER:8889/{cam_id}
    """
    width   = cam.get("width", 1280)
    height  = cam.get("height", 720)
    fps     = cam.get("fps", 15)
    bitrate = cam.get("bitrate", 2000)
    cam_id  = cam["id"]
    rtsp_target = f"rtsp://{MEDIA_SERVER}:{RTSP_PORT}/{cam_id}"

    if SHOW_LOCAL_WINDOWS:
        # Use GStreamer 'tee' to split stream: one to MediaMTX, one to local window
        if camera_source.startswith("rtspsrc"):
            return (
                f"{camera_source} ! "
                f"decodebin ! "
                f"videoconvert ! "
                f"tee name=t ! "
                f"queue ! "
                f"videoscale ! "
                f"video/x-raw,width={width},height={height} ! "
                f"videorate ! "
                f"video/x-raw,framerate={fps}/1 ! "
                f"x264enc tune=zerolatency bitrate={bitrate} speed-preset=ultrafast ! "
                f"video/x-h264,profile=baseline ! "
                f"rtspclientsink location={rtsp_target} protocols=tcp t. ! "
                f"queue ! "
                f"autovideosink sync=false"
            )
        else:
            return (
                f"{camera_source} ! "
                f"videoconvert ! "
                f"tee name=t ! "
                f"queue ! "
                f"videoscale ! "
                f"video/x-raw,width={width},height={height} ! "
                f"videorate ! "
                f"video/x-raw,framerate={fps}/1 ! "
                f"x264enc tune=zerolatency bitrate={bitrate} speed-preset=ultrafast ! "
                f"video/x-h264,profile=baseline ! "
                f"rtspclientsink location={rtsp_target} protocols=tcp t. ! "
                f"queue ! "
                f"autovideosink sync=false"
            )
    else:
        # Standard pipeline (no local display window)
        if camera_source.startswith("rtspsrc"):
            return (
                f"{camera_source} ! "
                f"decodebin ! "
                f"videoconvert ! "
                f"videoscale ! "
                f"video/x-raw,width={width},height={height} ! "
                f"videorate ! "
                f"video/x-raw,framerate={fps}/1 ! "
                f"x264enc tune=zerolatency bitrate={bitrate} speed-preset=ultrafast ! "
                f"video/x-h264,profile=baseline ! "
                f"rtspclientsink location={rtsp_target} protocols=tcp"
            )
        else:
            return (
                f"{camera_source} ! "
                f"videoconvert ! "
                f"videoscale ! "
                f"video/x-raw,width={width},height={height} ! "
                f"videorate ! "
                f"video/x-raw,framerate={fps}/1 ! "
                f"x264enc tune=zerolatency bitrate={bitrate} speed-preset=ultrafast ! "
                f"video/x-h264,profile=baseline ! "
                f"rtspclientsink location={rtsp_target} protocols=tcp"
            )


def run_pipeline(pipeline: str) -> int:
    """Launch gst-launch-1.0 as a subprocess. Returns exit code."""
    cmd = f"gst-launch-1.0 -v {pipeline}"
    print(f"[stream] Starting pipeline:\n  {cmd}\n")
    result = subprocess.run(cmd, shell=True)
    return result.returncode


def signal_handler(sig, frame):
    global _shutdown
    print("\n[stream] Shutdown signal received. Stopping...")
    _shutdown = True
    sys.exit(0)


def run_camera(cam: dict):
    """Run a single camera's streaming loop with reconnect."""
    global _shutdown
    cam_id = cam["id"]
    label  = cam["label"]

    try:
        camera_source = detect_camera_source(cam)
        print(f"  [{cam_id}] Camera src  : {camera_source}")
    except RuntimeError as e:
        print(f"[stream] ERROR for {cam_id}: {e}")
        return

    rtsp_target = f"rtsp://{MEDIA_SERVER}:{RTSP_PORT}/{cam_id}"
    pipeline = build_gstreamer_pipeline(cam, camera_source)

    attempt = 0
    while not _shutdown:
        attempt += 1
        print(f"\n[stream] [{cam_id}] Attempt #{attempt} — connecting to MediaMTX at {rtsp_target}")
        run_pipeline(pipeline)

        if _shutdown:
            break

        print(f"[stream] [{cam_id}] Pipeline stopped. Reconnecting in {RECONNECT_DELAY}s...")
        time.sleep(RECONNECT_DELAY)

    print(f"[stream] [{cam_id}] Exited cleanly.")


def main():
    global _shutdown

    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 60)
    print("  RoadShield Stream Service — Phase 1 (Multi-Camera)")
    print("=" * 60)
    print(f"  Cameras     : {len(CAMERAS)}")
    for cam in CAMERAS:
        rtsp_target = f"rtsp://{MEDIA_SERVER}:{RTSP_PORT}/{cam['id']}"
        print(f"  [{cam['id']}] {cam['label']}")
        print(f"          RTSP : {rtsp_target}")
        print(f"          Res  : {cam.get('width', 1280)}x{cam.get('height', 720)} @ {cam.get('fps', 15)}fps")
    print("=" * 60)

    if len(CAMERAS) == 1:
        # Single camera — run in main thread
        run_camera(CAMERAS[0])
    else:
        # Multi-camera — run each in its own thread
        threads = []
        for cam in CAMERAS:
            t = threading.Thread(target=run_camera, args=(cam,), daemon=True, name=f"stream-{cam['id']}")
            t.start()
            threads.append(t)
            print(f"  [{cam['id']}] Thread started")

        # Wait for all threads
        try:
            while not _shutdown:
                time.sleep(1)
        except KeyboardInterrupt:
            _shutdown = True

    print("[stream] Exited cleanly.")


if __name__ == "__main__":
    main()
