#!/usr/bin/env python3
"""
RoadShield AI — person_detect_poc.py
=====================================
Phase 0 proof-of-concept: person detection + tracking on the REAR_FIXED
camera, reported as events to the Laravel backend. This is deliberately
the smallest possible vertical slice of the full RoadShield AI spec —
no PPE, no cone/zone geometry, no PTZ investigation. It exists to prove
the Jetson can do real-time detection+tracking end to end without
touching the existing recording/streaming pipeline (stream.py,
camera-control.py, mediamtx.yml are all untouched and unaware this
script exists — it only reads an RTSP stream MediaMTX is already
publishing).

Uses the MAIN stream, not the sub-stream: benchmarked on this device,
YOLO's internal preprocessing resizes every frame to a fixed 640x640
before inference, so a 2688x1520 main-stream frame costs virtually the
same as a 640x360 sub-stream one (measured ~34ms vs ~35ms/frame) — but
the main stream gives far more real detail for small/distant subjects
(a person's PPE, a distant cone), which the sub-stream's already-low
640x360 source can't recover no matter how much a later crop is
upscaled.

One event + one snapshot is sent per NEW track id, not per frame.

Setup (see the RoadShield AI Phase 0 plan for why these exact versions —
confirmed working on this device: JetPack 6.2.3, CUDA 12.6, Orin Nano):
    sudo apt install nvidia-jetpack      # CUDA/cuDNN/TensorRT for this JetPack
    sudo apt install python3.10-venv python3-opencv
    python3 -m venv --system-site-packages ai/.venv   # --system-site-packages to reuse apt's cv2
    source ai/.venv/bin/activate
    pip install torch==2.8.0 torchvision==0.23.0 --index-url https://pypi.jetson-ai-lab.io/jp6/cu126
    pip install ultralytics requests lap   # lap is ByteTrack's tracker dependency —
                                            # ultralytics auto-installs it on first
                                            # .track() call if missing, but that happens
                                            # mid-run and needs a restart to take effect
    pip uninstall -y opencv-python   # ultralytics pulls a generic pip opencv build that
                                      # SHADOWS the system one above and lacks GStreamer
                                      # support — remove it so `import cv2` resolves back
                                      # to the --system-site-packages copy (verify with
                                      # `python -c "import cv2; print(cv2.getBuildInformation())"`
                                      # — GStreamer and FFMPEG should both say YES)
    pip install "numpy<2"    # torch's Jetson wheel + the system cv2 build are both
                              # compiled against NumPy 1.x; ultralytics otherwise pulls
                              # NumPy 2.x, which silently corrupts torch's C extensions

Requires Milestone A to have landed and at least one camera tagged
REAR_FIXED from the dashboard (so cameras_config.json has "role").

Usage:
    python3 ai/person_detect_poc.py [--url=https://controlroom.roadshield.ae]
"""

import datetime
import json
import os
import sys
import threading
import time

import cv2
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
CAMERAS_CONFIG_JSON = os.path.join(REPO_DIR, "cameras_config.json")
EVENT_TOKEN_JSON = os.path.join(REPO_DIR, "event_token.json")
SNAPSHOT_DIR = os.path.join(SCRIPT_DIR, "snapshots")

MEDIA_SERVER = "127.0.0.1"
RTSP_PORT = 8554
TARGET_FPS = 5.0
PERSON_CLASS_ID = 0  # COCO "person"

LARAVEL_URL = "https://controlroom.roadshield.ae"
for arg in sys.argv[1:]:
    if arg.startswith("--url="):
        LARAVEL_URL = arg.split("=", 1)[1]


def log(msg):
    print(f"[person-detect-poc] {msg}")


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def find_rear_fixed_camera():
    cameras = load_json(CAMERAS_CONFIG_JSON)
    if not cameras:
        log(f"{CAMERAS_CONFIG_JSON} not found or empty — run sync_camera_config.py first.")
        sys.exit(1)

    for key, cam in cameras.items():
        if cam.get("role") == "REAR_FIXED":
            return key, cam

    log("No camera with role REAR_FIXED found in cameras_config.json. "
        "Assign a REAR_FIXED role to a camera from the dashboard (Device Edit) "
        "and re-run sync_camera_config.py.")
    sys.exit(1)


def load_event_token():
    data = load_json(EVENT_TOKEN_JSON)
    if not data or not data.get("event_api_token"):
        log(f"{EVENT_TOKEN_JSON} not found — run sync_camera_config.py first "
            "(requires the device to be registered on the dashboard).")
        sys.exit(1)
    return data["event_api_token"]


def pick_capture_backend():
    """
    Inspect the actually-installed OpenCV build and choose a capture
    method accordingly, rather than assuming GStreamer/FFmpeg support is
    present — see the Phase 0 plan for why this must be checked, not
    guessed.

    FFMPEG is preferred over GStreamer when both are available: passing a
    bare RTSP URL to cv2's CAP_GSTREAMER backend relies on its own
    uridecodebin defaults (observed to fail here — "unable to start
    pipeline" / udpsrc errors, since it doesn't automatically use TCP the
    way this project's mediamtx.yml sourceProtocol: tcp does), whereas
    CAP_FFMPEG over TCP (forced below via OPENCV_FFMPEG_CAPTURE_OPTIONS)
    is the well-trodden path for a plain RTSP URL string.
    """
    build_info = cv2.getBuildInformation()
    has_gstreamer = "GStreamer:" in build_info and "YES" in build_info.split("GStreamer:")[1].split("\n")[0]
    has_ffmpeg = "FFMPEG:" in build_info and "YES" in build_info.split("FFMPEG:")[1].split("\n")[0]

    if has_ffmpeg:
        log("OpenCV build has FFMPEG support — using cv2.CAP_FFMPEG (RTSP over TCP).")
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        return cv2.CAP_FFMPEG
    if has_gstreamer:
        log("OpenCV build has GStreamer support (no FFMPEG) — using cv2.CAP_GSTREAMER.")
        return cv2.CAP_GSTREAMER

    log("WARNING: installed OpenCV build has neither GStreamer nor FFMPEG support. "
        "cv2.VideoCapture will likely fail to open the RTSP stream — see the Phase 0 "
        "plan's GStreamer-appsink fallback, not implemented in this PoC.")
    return cv2.CAP_ANY


class LatestFrameReader:
    """
    Reads an RTSP stream continuously in a background thread and always
    keeps only the newest frame — so a throttled consumer never processes
    a growing backlog of stale frames from a live stream.
    """

    def __init__(self, url, backend):
        self._cap = cv2.VideoCapture(url, backend)
        if not self._cap.isOpened():
            raise RuntimeError(f"Could not open RTSP stream: {url}")
        self._lock = threading.Lock()
        self._frame = None
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            ok, frame = self._cap.read()
            if not ok:
                time.sleep(0.5)
                continue
            with self._lock:
                self._frame = frame

    def latest(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def stop(self):
        self._running = False
        self._thread.join(timeout=2)
        self._cap.release()


def post_event(camera_key, track_id, confidence, snapshot_path, token):
    url = f"{LARAVEL_URL}/api/surveillance/events"
    headers = {"Authorization": f"Bearer {token}"}
    data = {
        "event_type": "PERSON_DETECTED",
        "camera_key": camera_key,
        "track_id": str(track_id),
        "confidence": f"{confidence:.4f}",
        "occurred_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    try:
        with open(snapshot_path, "rb") as f:
            files = {"snapshot": (os.path.basename(snapshot_path), f, "image/jpeg")}
            r = requests.post(url, headers=headers, data=data, files=files, timeout=10)
        if r.status_code == 200:
            log(f"Event posted for track {track_id} (confidence={confidence:.2f}).")
        else:
            log(f"Event POST failed: HTTP {r.status_code} — {r.text[:200]}")
    except Exception as e:
        log(f"Event POST failed: {e}")


def main():
    from ultralytics import YOLO

    camera_key, camera = find_rear_fixed_camera()
    event_token = load_event_token()
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    rtsp_url = f"rtsp://{MEDIA_SERVER}:{RTSP_PORT}/{camera_key}"  # main stream — see module docstring
    log(f"REAR_FIXED camera: {camera_key} ({camera.get('label')}) — {rtsp_url}")

    backend = pick_capture_backend()
    reader = LatestFrameReader(rtsp_url, backend)

    model = YOLO("yolov8n.pt")
    seen_track_ids = set()
    frame_interval = 1.0 / TARGET_FPS

    log(f"Running at ~{TARGET_FPS} FPS. Ctrl+C to stop.")
    try:
        while True:
            loop_start = time.time()

            frame = reader.latest()
            if frame is not None:
                results = model.track(frame, persist=True, classes=[PERSON_CLASS_ID], verbose=False)
                boxes = results[0].boxes
                if boxes is not None and boxes.id is not None:
                    for box, track_id, conf in zip(boxes.xyxy.tolist(), boxes.id.tolist(), boxes.conf.tolist()):
                        track_id = int(track_id)
                        if track_id in seen_track_ids:
                            continue
                        seen_track_ids.add(track_id)

                        x1, y1, x2, y2 = [int(v) for v in box]
                        annotated = frame.copy()
                        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
                        cv2.putText(annotated, f"person #{track_id} {conf:.2f}", (x1, max(0, y1 - 10)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                        snapshot_path = os.path.join(SNAPSHOT_DIR, f"{camera_key}_track{track_id}.jpg")
                        cv2.imwrite(snapshot_path, annotated)
                        log(f"New track #{track_id} (confidence={conf:.2f}) — snapshot saved.")
                        post_event(camera_key, track_id, conf, snapshot_path, event_token)

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, frame_interval - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        log("Stopped.")


if __name__ == "__main__":
    main()
