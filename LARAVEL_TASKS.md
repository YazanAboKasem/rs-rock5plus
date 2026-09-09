# Laravel Server — Required Changes & New Features

> This file documents all tasks needed on the **Laravel server** side.
> Reference project: Jetson camera streaming system (rs-python-stream).
> Jetson-side WebSocket client is already implemented and ready.

---

## Part 1: WebSocket Endpoint (Required for Real-Time Communication)

### Context

The Jetson currently polls the Laravel server via HTTP every 0.8s to check for PTZ commands
and every 2s for settings changes. This has been replaced on the Jetson side with a WebSocket
client that expects a persistent connection. The Jetson client has **automatic polling fallback**,
so existing functionality won't break — but WebSocket is needed for instant command delivery.

### Task 1.1: Set Up WebSocket Server

Choose one of these options (Laravel Reverb recommended for Laravel 11+):

- **Option A**: Laravel Reverb (built-in, recommended)
- **Option B**: `beyondcode/laravel-websockets` package
- **Option C**: Separate lightweight Node.js WebSocket server

The WebSocket endpoint must be available at:
```
wss://controlroom.roadshield.ae/ws/surveillance
```

### Task 1.2: Authentication

The Jetson connects with this header:
```
Authorization: Bearer b8e2ed9ae5def597e6a59f2801fca19fa758ab1a0cd3e9900b708b3aa357bc3c
```

The server must validate this token on WebSocket handshake (same token used for REST API).

### Task 1.3: Handle Incoming Connection

When the Jetson connects, it sends an identification message:
```json
{
    "event": "jetson.hello",
    "data": {
        "cameras": ["cam1", "cam2", "cam3"],
        "version": "2.0.0",
        "timestamp": 1720771200.0
    }
}
```

Store this connection and mark the Jetson as "online".

### Task 1.4: Send PTZ Commands via WebSocket

Currently the PTZ controller stores commands in cache and waits for polling.
Change it to send commands **directly** via the WebSocket connection:

**Current flow:**
```
Browser → POST /api/surveillance/cameras/{id}/ptz → Store in cache → Wait for poll
```

**New flow:**
```
Browser → POST /api/surveillance/cameras/{id}/ptz → Send via WebSocket instantly
```

Message format to send:
```json
{
    "event": "ptz.command",
    "data": {
        "camera_id": "cam1",
        "command_id": "unique-id-here",
        "action": "pan_left",
        "speed": 3
    }
}
```

The Jetson will respond with:
```json
{
    "event": "ptz.command.ack",
    "data": {
        "camera_id": "cam1",
        "command_id": "unique-id-here",
        "success": true,
        "error": null
    }
}
```

### Task 1.5: Send Settings Updates via WebSocket

When quality/FPS settings change, send them via WebSocket instead of waiting for poll:

```json
{
    "event": "settings.update",
    "data": {
        "cameras": {
            "cam1": {"quality": "hd", "fps": 15},
            "cam2": {"quality": "sd", "fps": 10}
        }
    }
}
```

The Jetson will respond with:
```json
{
    "event": "settings.update.ack",
    "data": {
        "status": "applied",
        "cameras": ["cam1", "cam2"]
    }
}
```

### Task 1.6: Keep Existing REST API Endpoints

**Do NOT remove** the existing polling endpoints — they serve as fallback:
- `GET  /api/surveillance/cameras/{id}/ptz/poll`
- `POST /api/surveillance/cameras/{id}/ptz/ack`
- `GET  /api/surveillance/cameras/settings`

### Task 1.7: Handle Heartbeat

The Jetson sends a heartbeat every 30 seconds:
```json
{
    "event": "heartbeat",
    "data": {"timestamp": 1720771200.0}
}
```

Use this to track Jetson online/offline status. If no heartbeat for >60 seconds,
mark the Jetson as "offline".

---

## Part 2: Test Mode Feature (New)

### Context

A diagnostic mode accessible from the Laravel dashboard that verifies the entire
streaming pipeline is working correctly: cameras → MediaMTX → Cloudflare Tunnel → Laravel.
This helps identify problems quickly without SSH-ing into the Jetson.

### Task 2.1: Test Mode Toggle in Dashboard

Add a "Test Mode" button/toggle in the surveillance dashboard UI.
When activated, it triggers a series of diagnostic checks on the Jetson
and displays results in real-time.

### Task 2.2: Send Test Mode Command via WebSocket

When the user activates Test Mode, send this event to the Jetson:
```json
{
    "event": "diagnostic.start",
    "data": {
        "checks": ["cameras", "streams", "tunnel", "logs"],
        "request_id": "diag-abc123"
    }
}
```

### Task 2.3: Camera Connectivity Check

The Jetson will check each camera and report back:
```json
{
    "event": "diagnostic.camera_status",
    "data": {
        "request_id": "diag-abc123",
        "cameras": {
            "cam1": {
                "ip": "192.168.1.64",
                "reachable": true,
                "rtsp_ok": true,
                "model": "Hikvision DS-2DE4A425IWG-E",
                "latency_ms": 12
            },
            "cam2": {
                "ip": "192.168.1.65",
                "reachable": false,
                "rtsp_ok": false,
                "error": "Connection refused",
                "fallback_active": true
            }
        }
    }
}
```

Display this as a status card per camera (green / red).

### Task 2.4: Fallback Video When Camera is Offline

When a camera is unreachable, the Jetson should automatically replace its stream
with a simple fallback video (e.g., a "Camera Offline" placeholder or a test pattern)
so the streaming pipeline doesn't break.

The dashboard should show:
- "Live" — camera is connected and streaming
- "Fallback" — camera is offline, showing placeholder video
- "Down" — stream is completely down

### Task 2.5: Stream Health Check

The Jetson checks if MediaMTX streams are active and reports:
```json
{
    "event": "diagnostic.stream_status",
    "data": {
        "request_id": "diag-abc123",
        "mediamtx_running": true,
        "mediamtx_pid": 1234,
        "streams": {
            "cam1":      {"active": true, "readers": 2, "source": "rtsp://192.168.1.64:554/..."},
            "cam1_live": {"active": true, "readers": 1, "transcoding": "hd@15fps"},
            "cam1_sub":  {"active": true, "readers": 0},
            "cam2":      {"active": false, "error": "source not found"},
            "cam2_live": {"active": false, "error": "no input"}
        }
    }
}
```

Display as a table showing each stream's status.

### Task 2.6: Cloudflare Tunnel Status

The Jetson checks the Cloudflare tunnel and reports:
```json
{
    "event": "diagnostic.tunnel_status",
    "data": {
        "request_id": "diag-abc123",
        "tunnel_running": true,
        "tunnel_pid": 5678,
        "tunnel_url": "https://abc-xyz.trycloudflare.com",
        "tunnel_accessible": true,
        "tunnel_latency_ms": 45,
        "error": null
    }
}
```

If there's an error:
```json
{
    "event": "diagnostic.tunnel_status",
    "data": {
        "request_id": "diag-abc123",
        "tunnel_running": false,
        "tunnel_pid": null,
        "tunnel_url": null,
        "tunnel_accessible": false,
        "error": "cloudflared process crashed — exit code 1: ERR Failed to connect to edge"
    }
}
```

Display the tunnel URL, status, and any errors prominently.

### Task 2.7: Log Viewer

The Jetson sends the last N lines of the system log:
```json
{
    "event": "diagnostic.logs",
    "data": {
        "request_id": "diag-abc123",
        "log_file": "system.log",
        "lines": [
            {"timestamp": "2026-07-12 12:30:00", "level": "INFO", "message": "MediaMTX started"},
            {"timestamp": "2026-07-12 12:30:02", "level": "ERROR", "message": "cam2: Connection refused"}
        ],
        "total_lines": 1500,
        "showing_last": 100
    }
}
```

Display in a scrollable log viewer with:
- Color coding by level (INFO=blue, WARN=yellow, ERROR=red)
- Auto-scroll to bottom
- Search/filter capability
- Option to request more lines

### Task 2.8: Test Mode Dashboard UI

Create a diagnostic panel/page that shows all the above in one view:

```
+-------------------------------------------------------------+
|  Test Mode — System Diagnostics                              |
+-------------------------------------------------------------+
|                                                              |
|  Cameras                                                     |
|  +----------+----------+----------+                          |
|  | cam1  OK | cam2  !! | cam3  OK |                          |
|  |Connected | Fallback |Connected |                          |
|  | 12ms     | Offline  | 8ms      |                          |
|  +----------+----------+----------+                          |
|                                                              |
|  Streams                                                     |
|  cam1 ------ active (2 readers, HD@15fps)                    |
|  cam1_live - active (1 reader, transcoding)                  |
|  cam2 ------ source not found                                |
|  cam3 ------ active (1 reader, HD@15fps)                     |
|                                                              |
|  Cloudflare Tunnel                                           |
|  Status: Running                                             |
|  URL: https://abc-xyz.trycloudflare.com                      |
|  Latency: 45ms                                               |
|                                                              |
|  Logs                                [Filter: ________]      |
|  +----------------------------------------------------------+|
|  | 12:30:00 INFO  MediaMTX started                          ||
|  | 12:30:02 ERROR cam2: Connection refused                  ||
|  | 12:30:02 WARN  cam2: Switching to fallback video         ||
|  | 12:30:05 INFO  Cloudflare tunnel URL received            ||
|  | 12:30:06 INFO  WebSocket connected to server             ||
|  +----------------------------------------------------------+|
|                                                              |
|  [Refresh All]  [Exit Test Mode]                             |
+-------------------------------------------------------------+
```

---

## Part 3: QNAP Recording Sync Feature (New)

### Context

The Jetson stores camera recordings locally (up to 300GB). The user needs the ability
to sync/backup these recordings to a QNAP NAS on an external network. This is triggered
from the Laravel dashboard — the user enters QNAP credentials and the Jetson uploads
the recordings in the background.

### Task 3.1: Sync Button in Dashboard

Add a "Sync Recordings" button in the surveillance dashboard.
When clicked, it opens a modal/form requesting QNAP connection details:

```
+-------------------------------------------------------------+
|  Sync Recordings to QNAP NAS                                |
+-------------------------------------------------------------+
|                                                              |
|  QNAP Host/IP:    [________________________]                |
|  Port:            [443_____]   Protocol: [HTTPS v]           |
|  Username:        [________________________]                 |
|  Password:        [________________________]                 |
|                                                              |
|  Remote Path:     [/Recordings/RoadShield/__]                |
|                                                              |
|  Sync Options:                                               |
|  [x] All recordings                                         |
|  [ ] Only today's recordings                                |
|  [ ] Only last N days:  [7___]                              |
|  [ ] Only specific cameras: [cam1] [cam2] [cam3]            |
|                                                              |
|  [x] Delete local files after successful upload             |
|  [ ] Overwrite existing files on QNAP                       |
|                                                              |
|  [Start Sync]  [Cancel]                                      |
+-------------------------------------------------------------+
```

### Task 3.2: Save QNAP Settings (Optional)

Allow the user to save QNAP settings in Laravel (encrypted) so they don't
have to re-enter credentials every time. Add a "Remember settings" checkbox.

Store in database:
- `qnap_host` — encrypted
- `qnap_port`
- `qnap_username` — encrypted
- `qnap_password` — encrypted
- `qnap_remote_path`

### Task 3.3: Send Sync Command via WebSocket

When the user clicks "Start Sync", Laravel sends this event to the Jetson:
```json
{
    "event": "sync.start",
    "data": {
        "request_id": "sync-abc123",
        "qnap": {
            "host": "nas.example.com",
            "port": 443,
            "protocol": "https",
            "username": "admin",
            "password": "secret123",
            "remote_path": "/Recordings/RoadShield/"
        },
        "options": {
            "scope": "all",
            "cameras": ["cam1", "cam2", "cam3"],
            "days": null,
            "delete_after_upload": true,
            "overwrite_existing": false
        }
    }
}
```

Scope options:
- `"all"` — sync all recordings
- `"today"` — only today's recordings
- `"last_n_days"` — last N days (use `days` field)
- `"cameras"` — only specific cameras (use `cameras` field)

### Task 3.4: Jetson Validates & Starts Upload

The Jetson first validates the QNAP connection and reports back:
```json
{
    "event": "sync.start.ack",
    "data": {
        "request_id": "sync-abc123",
        "status": "started",
        "total_files": 245,
        "total_size_gb": 42.5,
        "estimated_time_minutes": 35
    }
}
```

If validation fails:
```json
{
    "event": "sync.start.ack",
    "data": {
        "request_id": "sync-abc123",
        "status": "error",
        "error": "Cannot connect to QNAP: Authentication failed"
    }
}
```

### Task 3.5: Progress Updates

During upload, the Jetson sends periodic progress updates:
```json
{
    "event": "sync.progress",
    "data": {
        "request_id": "sync-abc123",
        "files_uploaded": 50,
        "files_total": 245,
        "bytes_uploaded": 5368709120,
        "bytes_total": 45634502656,
        "current_file": "recordings/cam1/2026-07-12/14-30-00.mp4",
        "speed_mbps": 25.4,
        "eta_seconds": 1200,
        "percent": 20.4
    }
}
```

### Task 3.6: Progress UI in Dashboard

Display a real-time progress panel when sync is active:

```
+-------------------------------------------------------------+
|  Syncing to QNAP NAS                                       |
+-------------------------------------------------------------+
|                                                              |
|  Progress: [==================------] 50/245 files (20.4%)  |
|                                                              |
|  Uploaded:  5.0 GB / 42.5 GB                                |
|  Speed:     25.4 Mbps                                       |
|  ETA:       20 minutes remaining                            |
|                                                              |
|  Current:   cam1/2026-07-12/14-30-00.mp4                    |
|                                                              |
|  [Pause]  [Cancel Sync]                                     |
+-------------------------------------------------------------+
```

### Task 3.7: Sync Completion / Error

When sync finishes:
```json
{
    "event": "sync.complete",
    "data": {
        "request_id": "sync-abc123",
        "status": "completed",
        "files_uploaded": 245,
        "files_failed": 2,
        "total_uploaded_gb": 42.3,
        "duration_minutes": 32,
        "failed_files": [
            {"file": "cam2/2026-07-10/corrupted.mp4", "error": "File read error"},
            {"file": "cam3/2026-07-11/08-00-00.mp4", "error": "QNAP disk full"}
        ],
        "local_files_deleted": 243
    }
}
```

### Task 3.8: Pause / Cancel Sync

Laravel can send pause or cancel commands:
```json
{
    "event": "sync.pause",
    "data": {"request_id": "sync-abc123"}
}
```
```json
{
    "event": "sync.cancel",
    "data": {"request_id": "sync-abc123"}
}
```

The Jetson responds:
```json
{
    "event": "sync.pause.ack",
    "data": {"request_id": "sync-abc123", "status": "paused", "files_uploaded": 50}
}
```

Resume:
```json
{
    "event": "sync.resume",
    "data": {"request_id": "sync-abc123"}
}
```

### Task 3.9: QNAP Upload Method on Jetson

The Jetson will upload files to QNAP using one of these protocols (in order of preference):
1. **SMB/CIFS** — mount QNAP share and copy files (`smbclient` or `mount -t cifs`)
2. **SFTP/SCP** — if QNAP has SSH enabled
3. **WebDAV** — QNAP supports WebDAV natively via HTTPS
4. **QNAP API** — FileStation API (`/cgi-bin/filemanager/utilRequest.cgi`)

The Jetson should auto-detect which method works based on the QNAP configuration.

---

## Part 4: Jetson-Side Implementation Status

### Already implemented (in rs-python-stream):
- WebSocket client (`jetson_ws_client.py`)
- PTZ command handler via WebSocket
- Settings update handler via WebSocket
- Auto-reconnect with exponential backoff
- Polling fallback when WebSocket is disconnected

### To implement on Jetson (after Laravel WebSocket is ready):
- `diagnostic.start` event handler
- Camera connectivity check (ping + RTSP probe)
- Fallback video injection when camera is offline
- MediaMTX stream health check (via MediaMTX API at http://localhost:9997)
- Cloudflare tunnel status check (process check + URL extraction)
- Log file reading and sending
- `sync.start` event handler — QNAP upload logic
- `sync.pause` / `sync.cancel` / `sync.resume` handlers
- Progress reporting during upload

---

## Event Types Summary

| Event | Direction | Purpose |
|---|---|---|
| `jetson.hello` | Jetson -> Server | Initial identification on connect |
| `heartbeat` | Jetson -> Server | Keep-alive every 30s |
| `ptz.command` | Server -> Jetson | PTZ movement command |
| `ptz.command.ack` | Jetson -> Server | PTZ execution result |
| `settings.update` | Server -> Jetson | Quality/FPS change |
| `settings.update.ack` | Jetson -> Server | Settings applied confirmation |
| `diagnostic.start` | Server -> Jetson | Trigger diagnostic checks |
| `diagnostic.camera_status` | Jetson -> Server | Camera connectivity results |
| `diagnostic.stream_status` | Jetson -> Server | MediaMTX stream health |
| `diagnostic.tunnel_status` | Jetson -> Server | Cloudflare tunnel status |
| `diagnostic.logs` | Jetson -> Server | System log lines |
| `sync.start` | Server -> Jetson | Start QNAP recording upload |
| `sync.start.ack` | Jetson -> Server | Upload validation result |
| `sync.progress` | Jetson -> Server | Upload progress update |
| `sync.complete` | Jetson -> Server | Upload finished (success/partial) |
| `sync.pause` | Server -> Jetson | Pause active upload |
| `sync.pause.ack` | Jetson -> Server | Pause confirmed |
| `sync.resume` | Server -> Jetson | Resume paused upload |
| `sync.resume.ack` | Jetson -> Server | Resume confirmed |
| `sync.cancel` | Server -> Jetson | Cancel active upload |
| `sync.cancel.ack` | Jetson -> Server | Cancel confirmed |
