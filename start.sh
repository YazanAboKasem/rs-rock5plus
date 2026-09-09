#!/usr/bin/env bash
# =============================================================================
# RoadShield — Smart Surveillance Startup Script
# Phase 1: Starts MediaMTX + Python stream service
#
# Usage:
#   ./start.sh
#   ./start.sh --no-download   (skip MediaMTX auto-download)
#
# HOW TO CHANGE SERVER FOR DEPLOYMENT:
#   Edit MEDIA_SERVER in stream.py — this script reads nothing about the server.
#   To run on Jetson: scp this whole folder, run ./start.sh there.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MEDIAMTX_VERSION="v1.9.3"
MEDIAMTX_BIN="./mediamtx"
MEDIAMTX_CONFIG="./mediamtx.yml"
PYTHON_SCRIPT="./stream.py"
MEDIAMTX_PID=""
STREAM_PID=""
CAMERA_CTRL_PID=""
REC_CLEANUP_PID=""

# ─── Color output ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${GREEN}[start.sh]${NC} $*"; }
warn() { echo -e "${YELLOW}[start.sh]${NC} $*"; }
err()  { echo -e "${RED}[start.sh] ERROR:${NC} $*" >&2; }

# ─── Cleanup on exit ─────────────────────────────────────────────────────────
cleanup() {
    echo ""
    log "Shutting down services..."
    if [[ -n "$REC_CLEANUP_PID" ]] && kill -0 "$REC_CLEANUP_PID" 2>/dev/null; then
        kill "$REC_CLEANUP_PID" 2>/dev/null && log "recording-cleanup.py stopped."
    fi
    if [[ -n "$CAMERA_CTRL_PID" ]] && kill -0 "$CAMERA_CTRL_PID" 2>/dev/null; then
        kill "$CAMERA_CTRL_PID" 2>/dev/null && log "camera-control.py stopped."
    fi
    if [[ -n "$STREAM_PID" ]] && kill -0 "$STREAM_PID" 2>/dev/null; then
        kill "$STREAM_PID" 2>/dev/null && log "stream.py stopped."
    fi
    if [[ -n "$MEDIAMTX_PID" ]] && kill -0 "$MEDIAMTX_PID" 2>/dev/null; then
        kill "$MEDIAMTX_PID" 2>/dev/null && log "MediaMTX stopped."
    fi
    log "Done."
    exit 0
}
trap cleanup INT TERM

# ─── Detect OS & arch ─────────────────────────────────────────────────────────
detect_platform() {
    local os arch
    case "$(uname -s)" in
        Linux*)  os="linux"  ;;
        Darwin*) os="darwin" ;;
        *)       err "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac
    case "$(uname -m)" in
        x86_64)  arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        armv7l)  arch="armv7" ;;
        *)       err "Unsupported arch: $(uname -m)"; exit 1 ;;
    esac
    echo "${os}_${arch}"
}

# ─── Download MediaMTX if not present ────────────────────────────────────────
download_mediamtx() {
    if [[ "$1" == "--no-download" ]]; then
        if [[ ! -f "$MEDIAMTX_BIN" ]]; then
            err "MediaMTX binary not found at $MEDIAMTX_BIN and --no-download was set."
            exit 1
        fi
        return
    fi

    if [[ -f "$MEDIAMTX_BIN" ]]; then
        log "MediaMTX binary already present. Skipping download."
        return
    fi

    local platform
    platform="$(detect_platform)"
    local url="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_${platform}.tar.gz"
    local tarball="mediamtx_tmp.tar.gz"

    log "Downloading MediaMTX ${MEDIAMTX_VERSION} for ${platform}..."
    log "URL: $url"

    if command -v curl &>/dev/null; then
        curl -fsSL -o "$tarball" "$url"
    elif command -v wget &>/dev/null; then
        wget -q -O "$tarball" "$url"
    else
        err "curl or wget required to download MediaMTX."
        exit 1
    fi

    tar -xzf "$tarball" mediamtx
    rm -f "$tarball"
    chmod +x "$MEDIAMTX_BIN"
    log "MediaMTX downloaded and extracted."
}

# ─── Verify Python ────────────────────────────────────────────────────────────
check_python() {
    if ! command -v python3 &>/dev/null; then
        err "python3 not found. Please install Python 3."
        exit 1
    fi
    local ver
    ver="$(python3 --version 2>&1)"
    log "Python: $ver"
}

# ─── Main ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${BLUE}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${BLUE}║  RoadShield Smart Surveillance — Phase 1         ║${NC}"
echo -e "${BOLD}${BLUE}╚══════════════════════════════════════════════════╝${NC}"
echo ""

NO_DOWNLOAD="${1:-}"
download_mediamtx "$NO_DOWNLOAD"
check_python

# Start MediaMTX
log "Starting MediaMTX..."
"$MEDIAMTX_BIN" "$MEDIAMTX_CONFIG" &
MEDIAMTX_PID=$!
log "MediaMTX PID: $MEDIAMTX_PID"

# Give MediaMTX a moment to initialize
sleep 2

# Check MediaMTX is still running
if ! kill -0 "$MEDIAMTX_PID" 2>/dev/null; then
    err "MediaMTX failed to start. Check mediamtx.yml."
    exit 1
fi

log "MediaMTX running on:"
log "  RTSP   → rtsp://127.0.0.1:8554/cam1"
log "  HLS    → http://127.0.0.1:8888/cam1/index.m3u8"
log "  WebRTC → http://127.0.0.1:8889/cam1"
log "  API    → http://127.0.0.1:9997"
echo ""

# Start Python stream
log "Starting stream.py..."
python3 "$PYTHON_SCRIPT" &
STREAM_PID=$!
log "stream.py PID: $STREAM_PID"
echo ""

# Start camera-control.py (PTZ + Quality/FPS transcoding agent)
if [[ -f "camera-control.py" ]]; then
    log "Starting camera-control.py (PTZ + transcoding agent)..."
    python3 camera-control.py > /tmp/camera-control.log 2>&1 &
    CAMERA_CTRL_PID=$!
    sleep 1
    if kill -0 "$CAMERA_CTRL_PID" 2>/dev/null; then
        log "camera-control.py running (PID: $CAMERA_CTRL_PID)"
    else
        warn "camera-control.py failed to start. Check: /tmp/camera-control.log"
        CAMERA_CTRL_PID=""
    fi
else
    warn "camera-control.py not found — PTZ and quality controls disabled."
fi
echo ""

# Start recording-cleanup.py (disk space management)
if [[ -f "recording-cleanup.py" ]]; then
    log "Starting recording-cleanup.py (recording disk management)..."
    python3 recording-cleanup.py > /tmp/recording-cleanup.log 2>&1 &
    REC_CLEANUP_PID=$!
    sleep 1
    if kill -0 "$REC_CLEANUP_PID" 2>/dev/null; then
        log "recording-cleanup.py running (PID: $REC_CLEANUP_PID)"
    else
        warn "recording-cleanup.py failed to start. Check: /tmp/recording-cleanup.log"
        REC_CLEANUP_PID=""
    fi
else
    warn "recording-cleanup.py not found — recording disk management disabled."
fi
echo ""

log "System is running. Press Ctrl+C to stop all services."
echo ""

# Wait for any process to exit
wait "$STREAM_PID" "$MEDIAMTX_PID"
