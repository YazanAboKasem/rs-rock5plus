# python-stream — RoadShield Smart Surveillance

Phase 1 Python streaming service. Reads a camera, encodes H.264, and pushes a live RTSP stream to MediaMTX.

---

## Architecture

```
USB/IP Camera
     ↓
stream.py  (GStreamer pipeline)
     ↓  RTSP publish → rtsp://127.0.0.1:8554/cam1
MediaMTX
     ├── RTSP  → rtsp://127.0.0.1:8554/cam1
     ├── HLS   → http://127.0.0.1:8888/cam1/index.m3u8
     └── WebRTC → http://127.0.0.1:8889/cam1
```

Laravel dashboard connects the browser directly to the HLS/WebRTC URL.
**Laravel never touches video bytes.**

---

## Quick Start

```bash
# 1. Install GStreamer (system package — NOT pip)

# macOS
brew install gstreamer gst-plugins-base gst-plugins-good \
             gst-plugins-bad gst-plugins-ugly

# Ubuntu / Jetson (Debian-based)
sudo apt install gstreamer1.0-tools \
                 gstreamer1.0-plugins-base \
                 gstreamer1.0-plugins-good \
                 gstreamer1.0-plugins-bad \
                 gstreamer1.0-plugins-ugly

# 2. Run (auto-downloads MediaMTX on first run)
chmod +x start.sh
./start.sh
```

---

## Ports

| Service   | Port | Protocol | URL                                        |
|-----------|------|----------|--------------------------------------------|
| RTSP      | 8554 | TCP      | `rtsp://127.0.0.1:8554/cam1`              |
| HLS       | 8888 | HTTP     | `http://127.0.0.1:8888/cam1/index.m3u8`   |
| WebRTC    | 8889 | HTTP/UDP  | `http://127.0.0.1:8889/cam1`              |
| API/UI    | 9997 | HTTP     | `http://127.0.0.1:9997`                   |

---

## Configuration

### Changing the server (deployment)

Open `stream.py` and change **one variable**:

```python
# Line 22 — stream.py
MEDIA_SERVER = "127.0.0.1"      # ← change this
```

| Scenario              | Value                        |
|-----------------------|------------------------------|
| Local development     | `"127.0.0.1"`               |
| Jetson on LAN         | `"192.168.1.x"`             |
| Public server         | `"your-server-ip"`          |
| Domain name           | `"stream.yourdomain.com"`   |

### Camera source

By default, the script auto-detects the OS:
- **Linux / Jetson** → `/dev/video0` (V4L2)
- **macOS** → built-in webcam (AVFoundation)

To use an **RTSP IP camera** instead, edit `stream.py`:
```python
# Uncomment this line and comment out the auto-detection block:
CAMERA_SOURCE_RTSP = "rtspsrc location=rtsp://admin:password@192.168.1.64:554/..."
```

---

## Folder Structure

```
python-stream/
├── stream.py          ← Camera reader + GStreamer pipeline
├── mediamtx.yml       ← MediaMTX server configuration
├── start.sh           ← One-command startup script
├── requirements.txt   ← Python deps (none for Phase 1)
└── README.md
```

---

## Verifying the Stream

```bash
# Test RTSP with VLC
vlc rtsp://127.0.0.1:8554/cam1

# Test HLS in browser — open this URL:
# http://127.0.0.1:8888/cam1/index.m3u8

# MediaMTX built-in API (JSON status)
curl http://127.0.0.1:9997/v3/paths/list
```

---

## Deploying to Jetson

```bash
# On your Mac — copy project to Jetson
scp -r python-stream/ jetson@JETSON_IP:/home/jetson/

# On Jetson
ssh jetson@JETSON_IP
cd ~/python-stream
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-good \
                 gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
# Edit stream.py: set MEDIA_SERVER to Jetson's LAN IP
chmod +x start.sh
./start.sh
```

---

## Future Phases

- **AI Processing**: Add OpenCV + model inference in `stream.py`. POST alerts to Laravel API at `/api/surveillance/events`.
- **Multiple cameras**: Duplicate `stream.py` with different `STREAM_PATH` values (`cam2`, `cam3`...). Add corresponding paths to `mediamtx.yml`.
- **Auth**: Add `publishUser`/`publishPass` to `mediamtx.yml` paths section.
- **TLS**: Add `rtspsEncryption: strict` + certificate paths to `mediamtx.yml`.
