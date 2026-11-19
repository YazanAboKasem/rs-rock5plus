#!/usr/bin/env bash
# =============================================================================
# RoadShield — Connect Local MediaMTX to Remote Laravel Dashboard
# =============================================================================
#
# ما الذي يفعله هذا السكريبت:
#   1. يشغّل MediaMTX (يقرأ من كاميرات Hikvision عبر RTSP)
#   2. يشغّل Cloudflare Tunnel (يعطي المتصفح عنوان HTTPS عام)
#   3. يرسل URL الـ Tunnel تلقائياً لـ Laravel API
#   4. عند الإيقاف (Ctrl+C) يُلغي تسجيل الـ URL
#
# لا تحتاج تعدّل .env أو تشغّل config:cache بعد الآن.
# =============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# =============================================================================
# ▼  CONFIG — عدّل هذه القيم فقط عند تغيير السيرفر  ▼
# =============================================================================

LARAVEL_URL="https://controlroom.roadshield.ae"
SURVEILLANCE_TOKEN="b8e2ed9ae5def597e6a59f2801fca19fa758ab1a0cd3e9900b708b3aa357bc3c"

# Auto-generated once per device on first run (~/.roadshield/device_id) — never
# hand-edit this. This is what makes it safe to clone this exact codebase onto
# any number of other devices: each one gets its own unique id automatically,
# so nothing collides. See device_identity.py.
SERVER_ID="$(python3 "$SCRIPT_DIR/device_identity.py")"

HLS_PORT=8888
MEDIAMTX_BIN="./mediamtx"
MEDIAMTX_CONFIG="./mediamtx.yml"
TUNNEL_LOG="/tmp/cloudflared-mediamtx.log"

# =============================================================================

MEDIAMTX_PID=""
TUNNEL_PID=""
CAMERA_CTRL_PID=""
REC_CLEANUP_PID=""
DEVICE_STATS_PID=""

# ─── Colors ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BLUE='\033[0;34m'; BOLD='\033[1m'; CYAN='\033[0;36m'; NC='\033[0m'

log()     { echo -e "${GREEN}[connect]${NC} $*"; }
info()    { echo -e "${BLUE}[connect]${NC} $*"; }
warn()    { echo -e "${YELLOW}[connect]${NC} $*"; }
success() { echo -e "${BOLD}${GREEN}[connect]${NC} $*"; }
err()     { echo -e "${RED}[connect] ERROR:${NC} $*" >&2; }

# ─── Register tunnel URL with Laravel API ────────────────────────────────────
register_tunnel() {
    local url="$1"
    local endpoint="${LARAVEL_URL}/api/surveillance/register-tunnel"

    log "تسجيل URL في Laravel API للجهاز (${SERVER_ID})..."

    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$endpoint" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${SURVEILLANCE_TOKEN}" \
        -d "{\"hls_url\": \"${url}\", \"jetson_name\": \"${SERVER_ID}\"}" \
        --connect-timeout 10 \
        --max-time 15 2>/dev/null || echo -e "\nCURL_FAILED")

    local http_body http_code
    http_body=$(echo "$response" | sed '$d')   # all lines except last (macOS + Linux)
    http_code=$(echo "$response" | tail -n 1)

    if [[ "$http_code" == "200" ]]; then
        success "✅ URL مسجّل بنجاح في Laravel للجهاز (${SERVER_ID})!"
        log "  → الكاميرات تعمل على: ${LARAVEL_URL}/surveillance"
    elif [[ "$http_code" == "401" ]]; then
        err "❌ Unauthorized — تأكد أن SURVEILLANCE_TOKEN يطابق .env على السيرفر"
        err "  Token محلي: ${SURVEILLANCE_TOKEN:0:8}..."
    elif [[ "$http_code" == "CURL_FAILED" ]]; then
        err "❌ تعذّر الوصول لـ Laravel: ${endpoint}"
        err "  تأكد أن السيرفر يعمل ومتصل بالإنترنت"
    else
        warn "⚠️  استجابة غير متوقعة (HTTP ${http_code}): ${http_body}"
    fi
}

# ─── Clear tunnel URL from Laravel cache ─────────────────────────────────────
clear_tunnel() {
    local endpoint="${LARAVEL_URL}/api/surveillance/register-tunnel"
    log "إلغاء تسجيل URL من Laravel..."
    curl -s -X DELETE "$endpoint" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${SURVEILLANCE_TOKEN}" \
        -d "{\"jetson_name\": \"${SERVER_ID}\"}" \
        --connect-timeout 5 \
        --max-time 10 2>/dev/null && log "URL أُلغي تسجيله." || true
}

# ─── Cleanup on Ctrl+C ───────────────────────────────────────────────────────
cleanup() {
    local exit_code="${1:-0}"
    echo ""
    log "إيقاف النظام..."
    clear_tunnel
    [[ -n "$DEVICE_STATS_PID" ]] && kill "$DEVICE_STATS_PID" 2>/dev/null && log "device_stats.py متوقف."
    [[ -n "$REC_CLEANUP_PID" ]] && kill "$REC_CLEANUP_PID" 2>/dev/null && log "recording-cleanup.py متوقف."
    [[ -n "$CAMERA_CTRL_PID" ]] && kill "$CAMERA_CTRL_PID" 2>/dev/null && log "camera-control.py متوقف."
    [[ -n "$TUNNEL_PID"      ]] && kill "$TUNNEL_PID"      2>/dev/null && log "Cloudflare Tunnel متوقف."
    [[ -n "$MEDIAMTX_PID"   ]] && kill "$MEDIAMTX_PID"   2>/dev/null && log "MediaMTX متوقف."
    log "تم الإيقاف بنجاح."
    exit "$exit_code"
}
trap cleanup INT TERM

# ─── Check deps ───────────────────────────────────────────────────────────────
if ! command -v cloudflared &>/dev/null; then
    err "cloudflared غير مثبت. ثبّته: brew install cloudflared"
    exit 1
fi
if ! command -v curl &>/dev/null; then
    err "curl غير مثبت."
    exit 1
fi

# ─── Download MediaMTX if missing ────────────────────────────────────────────
if [[ ! -f "$MEDIAMTX_BIN" ]]; then
    warn "MediaMTX غير موجود — يتم التحميل..."
    MEDIAMTX_VERSION="v1.9.3"
    
    # OS detection
    OS="linux"
    case "$(uname -s)" in
        Darwin*) OS="darwin" ;;
        Linux*)  OS="linux"  ;;
    esac
    
    # Arch detection
    ARCH="amd64"
    case "$(uname -m)" in
        x86_64)        ARCH="amd64" ;;
        aarch64|arm64) ARCH="arm64" ;;
        armv7l)        ARCH="armv7" ;;
    esac
    
    URL_DL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_${OS}_${ARCH}.tar.gz"
    curl -fsSL -o mediamtx_tmp.tar.gz "$URL_DL"
    tar -xzf mediamtx_tmp.tar.gz mediamtx
    rm -f mediamtx_tmp.tar.gz
    chmod +x mediamtx
    log "MediaMTX جاهز."
fi

# ─── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║  RoadShield Smart Surveillance — Auto-Connect               ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
info "السيرفر: ${LARAVEL_URL}"
info "المحلي:  MediaMTX ← كاميرات Hikvision"
echo ""

# ─── 0. Sync camera config from dashboard (IP/creds/PTZ) ─────────────────────
# No-op if this device isn't registered yet, or has no cameras configured —
# only rewrites mediamtx.yml when there's real camera data to write.
if command -v python3 &>/dev/null && [[ -f "sync_camera_config.py" ]]; then
    log "مزامنة إعدادات الكاميرات من الداشبورد..."
    python3 sync_camera_config.py --url="$LARAVEL_URL" --token="$SURVEILLANCE_TOKEN" --server-name="$SERVER_ID" || warn "تعذّرت مزامنة إعدادات الكاميرات — سيُستخدم الإعداد المحلي الحالي."
fi
echo ""

# ─── 1. Start MediaMTX ────────────────────────────────────────────────────────
log "تشغيل MediaMTX..."

# Kill any stale MediaMTX / cloudflared from a previous run
pkill -f "mediamtx" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1   # wait for ports to free up

"$MEDIAMTX_BIN" "$MEDIAMTX_CONFIG" &
MEDIAMTX_PID=$!
sleep 2

if ! kill -0 "$MEDIAMTX_PID" 2>/dev/null; then
    err "MediaMTX فشل. راجع mediamtx.yml"
    exit 1
fi
log "MediaMTX يعمل (PID: $MEDIAMTX_PID)"

# ─── 1b. Start camera-control.py (PTZ agent) ─────────────────────────────────
if command -v python3 &>/dev/null && [[ -f "camera-control.py" ]]; then
    log "تشغيل camera-control.py (PTZ agent)..."
    python3 camera-control.py --url="$LARAVEL_URL" --token="$SURVEILLANCE_TOKEN" --server-name="$SERVER_ID" > /tmp/camera-control.log 2>&1 &
    CAMERA_CTRL_PID=$!
    sleep 1
    if kill -0 "$CAMERA_CTRL_PID" 2>/dev/null; then
        log "camera-control.py يعمل (PID: $CAMERA_CTRL_PID)"
    else
        warn "camera-control.py فشل في التشغيل. تحقق من: /tmp/camera-control.log"
        CAMERA_CTRL_PID=""
    fi
else
    warn "camera-control.py غير موجود أو python3 غير متاح — PTZ غير مفعّل."
fi
echo ""

# ─── 1c. Start recording-cleanup.py (recording disk management) ──────────────
if command -v python3 &>/dev/null && [[ -f "recording-cleanup.py" ]]; then
    log "تشغيل recording-cleanup.py (حجم التسجيلات)..."
    python3 recording-cleanup.py > /tmp/recording-cleanup.log 2>&1 &
    REC_CLEANUP_PID=$!
    sleep 1
    if kill -0 "$REC_CLEANUP_PID" 2>/dev/null; then
        log "recording-cleanup.py يعمل (PID: $REC_CLEANUP_PID)"
    else
        warn "recording-cleanup.py فشل في التشغيل. تحقق من: /tmp/recording-cleanup.log"
        REC_CLEANUP_PID=""
    fi
else
    warn "recording-cleanup.py غير موجود — إدارة القرص غير مفعّلة."
fi
echo ""

# ─── 1d. Start device_stats.py (CPU/RAM/temperature reporting) ───────────────
# GPS auto-detection: a Quectel EC25-style USB LTE+GPS modem exposes NMEA on
# USB interface 01 and AT commands on interface 02. Resolved via the stable
# /dev/serial/by-id symlinks (tied to the USB interface number) rather than
# /dev/ttyUSBn — once ModemManager manages this modem for internet access it
# can reprobe/reset it and shift the ttyUSBn numbering across boots, which
# silently broke GPS when it was hardcoded to ttyUSB1/ttyUSB3.
# The glob itself (not the resolved link) is passed to device_stats.py: the
# by-id name embeds the modem's serial number, so swapping the modem changes
# it — GpsReader re-resolves the glob on every reopen.
GPS_ARGS=()
GPS_NMEA_GLOB='/dev/serial/by-id/usb-Quectel_*-if01-port0'
GPS_AT_GLOB='/dev/serial/by-id/usb-Quectel_*-if02-port0'
GPS_NMEA_LINK=$(ls $GPS_NMEA_GLOB 2>/dev/null | head -1)
if [[ -n "$GPS_NMEA_LINK" ]]; then
    GPS_ARGS=(--gps-port="$GPS_NMEA_GLOB" --gps-baud=115200 --gps-at-port="$GPS_AT_GLOB" --gps-at-baud=115200)
    log "مودم Quectel مكتشف — تفعيل GPS على $GPS_NMEA_LINK"
fi

if command -v python3 &>/dev/null && [[ -f "device_stats.py" ]]; then
    log "تشغيل device_stats.py (مراقبة الموارد)..."
    python3 device_stats.py --url="$LARAVEL_URL" --token="$SURVEILLANCE_TOKEN" --server-name="$SERVER_ID" "${GPS_ARGS[@]}" > /tmp/device-stats.log 2>&1 &
    DEVICE_STATS_PID=$!
    sleep 1
    if kill -0 "$DEVICE_STATS_PID" 2>/dev/null; then
        log "device_stats.py يعمل (PID: $DEVICE_STATS_PID)"
    else
        warn "device_stats.py فشل في التشغيل. تحقق من: /tmp/device-stats.log"
        DEVICE_STATS_PID=""
    fi
else
    warn "device_stats.py غير موجود أو python3 غير متاح — مراقبة الموارد غير مفعّلة."
fi
echo ""

# ─── 2. Start Cloudflare Tunnel ───────────────────────────────────────────────
log "تشغيل Cloudflare Tunnel..."
rm -f "$TUNNEL_LOG"

# Redirect stdout+stderr directly to log file.
# IMPORTANT: do NOT use --logfile — it outputs JSON format which hides the URL banner.
# Direct redirect captures the human-readable text format where the URL appears.
cloudflared tunnel \
    --url "http://localhost:${HLS_PORT}" \
    --no-autoupdate \
    > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

# Show cloudflared output in terminal as it arrives (background)
tail -f "$TUNNEL_LOG" 2>/dev/null &
TAIL_PID=$!

# Poll log for URL — up to 45 seconds
log "انتظار URL من Cloudflare..."
TUNNEL_URL=""
for i in $(seq 1 45); do
    sleep 1
    if [[ -f "$TUNNEL_LOG" ]]; then
        # Try plain-text format first (when cloudflared output is not a TTY)
        TUNNEL_URL=$(grep -oP 'https://[a-zA-Z0-9\-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)
        # Fallback: JSON format — URL is inside a "message" field
        if [[ -z "$TUNNEL_URL" ]]; then
            TUNNEL_URL=$(python3 -c "
import sys, re, json
for line in open('$TUNNEL_LOG'):
    try:
        msg = json.loads(line).get('message','')
    except:
        msg = line
    m = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', msg)
    if m: print(m.group(0)); break
" 2>/dev/null || true)
        fi
        [[ -n "$TUNNEL_URL" ]] && break
    fi
    (( i % 10 == 0 )) && log "  جاري الانتظار... ($i/45 ثانية)"
done

# Stop tail once we have the URL (or timed out)
kill "$TAIL_PID" 2>/dev/null || true
wait "$TAIL_PID" 2>/dev/null || true

if [[ -z "$TUNNEL_URL" ]]; then
    err "❌ تعذّر استلام URL تلقائياً (ربما بسبب انقطاع الإنترنت أو DNS)."
    err "تحقق من: $TUNNEL_LOG"
    err "سيتم إيقاف السكريبت لتتمكن خدمة systemd من إعادة المحاولة لاحقاً..."
    cleanup 1
else
    # ─── 3. Register URL with Laravel API ─────────────────────────────────────
    echo ""
    echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  Tunnel URL:${NC} ${CYAN}${TUNNEL_URL}${NC}"
    echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    register_tunnel "$TUNNEL_URL"

    echo ""
    echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  Dashboard:${NC} ${CYAN}${LARAVEL_URL}/surveillance${NC}"
    echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
fi

log "النظام يعمل. اضغط Ctrl+C للإيقاف."
echo ""

# Wait for either process to exit
wait -n "$MEDIAMTX_PID" "$TUNNEL_PID" || true
warn "أحد البرامج الأساسية توقف عن العمل (MediaMTX أو Cloudflare). جاري إعادة التشغيل..."
cleanup 1
