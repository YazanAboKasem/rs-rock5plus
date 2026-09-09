# RoadShield QNAP Background Sync Agent

This background agent runs permanently on your QNAP NAS (either directly or via Docker) to periodically scan and download recorded surveillance videos from the remote Laravel VPS server. Once a video is downloaded and verified, the agent notifies the Laravel server to delete the copy on the VPS, saving server disk space.

## Features

- **Daemon Operation**: Runs continuously with a configurable sync interval (default is `60` seconds).
- **Concurrent Downloads**: Uses Python's `ThreadPoolExecutor` to download multiple video files in parallel.
- **Fail-Safe Uploads**: Downloads to `.tmp` files and swaps them only after size matching verification to prevent corrupted videos.
- **Auto-Retries**: Retries temporary connection timeouts up to `3` times with a delay.
- **Persistent Logging**: Writes logs to console and a rotating local file at `logs/agent.log` (keeps up to 5 historical logs of 10MB each).
- **Laravel Cleanup Notification**: Triggers `POST /api/surveillance/recordings/download-complete` to delete downloaded files from the VPS.

---

## Installation & Setup

### 1. Configure the Agent

Copy the configuration template `config.json.example` to `config.json` in the same directory:

```bash
cp config.json.example config.json
```

Open `config.json` and adjust the parameters:

```json
{
  "server": "https://controlroom.roadshield.ae",
  "token": "b8e2ed9ae5def597e6a59f2801fca19fa758ab1a0cd3e9900b708b3aa357bc3c",
  "download_path": "/share/Recordings",
  "sync_interval_seconds": 60,
  "max_concurrent_downloads": 3,
  "max_download_retries": 3,
  "retry_delay_seconds": 5
}
```

- `server`: The domain name where the RoadShield control room dashboard is hosted.
- `token`: The authentication Bearer API token.
- `download_path`: The directory on your QNAP NAS where video files should be saved.

### 2. Run Directly on QNAP (Python 3)

Ensure Python 3 is installed on your QNAP NAS (available via the App Center or Entware).

1. Install dependencies:
   ```bash
   pip install requests
   ```

2. Run the agent:
   ```bash
   chmod +x qnap_agent.py
   python3 qnap_agent.py
   ```

To run it permanently in the background:
```bash
nohup python3 qnap_agent.py >/dev/null 2>&1 &
```

---

## Run via Docker (Recommended for QNAP Container Station)

If you use **Container Station** on QNAP, running the agent in a Docker container is the most stable and modern option.

### 1. Dockerfile

Create a `Dockerfile` in the agent folder:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir requests

# Copy source code
COPY qnap_agent.py /app/
COPY config.json /app/

# Run the agent
CMD ["python", "-u", "qnap_agent.py"]
```

### 2. Build and Run

Build the Docker image:
```bash
docker build -t roadshield-qnap-agent .
```

Run the container in the background, mounting your QNAP shared folder (e.g. `/share/Recordings`) so downloaded videos are stored persistently:

```bash
docker run -d \
  --name roadshield-qnap-agent \
  --restart unless-stopped \
  -v /share/Recordings:/share/Recordings \
  roadshield-qnap-agent
```
*(Ensure the mount path inside the container matches the `download_path` defined in `config.json`)*
