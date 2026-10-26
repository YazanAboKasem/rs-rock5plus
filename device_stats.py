#!/usr/bin/env python3
"""
RoadShield — device_stats.py
================================
Periodically reads local CPU / RAM / disk / temperature usage and reports
it to the Laravel dashboard so the "Test Mode — Device Resources" panel
has live data to show.

POSTs to: {LARAVEL_URL}/api/device-agent/heartbeat
"""

import os
import time
import json
import logging
import platform
import signal
import socket
import subprocess
import sys
from urllib.parse import urlparse

import requests

from device_identity import get_device_id
from gps_service import GpsReader, enable_modem_gps

try:
    import psutil
except ImportError:
    print("psutil is required: pip3 install psutil", file=sys.stderr)
    sys.exit(1)

# ─── Configuration ──────────────────────────────────────────────────────────
LARAVEL_URL         = "https://controlroom.roadshield.ae"
SURVEILLANCE_TOKEN  = "b8e2ed9ae5def597e6a59f2801fca19fa758ab1a0cd3e9900b708b3aa357bc3c"
SERVER_ID           = get_device_id()  # unique per device, auto-generated — never hand-edit this
HEARTBEAT_INTERVAL  = 10       # seconds

# Remote Terminal (reverse SSH tunnel) settings — see handle_open_terminal().
SSH_HOST      = urlparse(LARAVEL_URL).hostname
SSH_PORT      = 22
SSH_USER      = "device-agent"
SSH_KEY_PATH  = os.path.expanduser("~/.roadshield/terminal_key")

# GPS — optional, only active when --gps-port is passed (different device
# types expose GPS differently: Jetson onboard UART is /dev/ttyTHS1 @ 9600;
# a Quectel EC25-style USB LTE+GPS modem exposes NMEA on /dev/ttyUSB1 @ 115200).
GPS_PORT = None
GPS_BAUD = 9600
# For a Quectel-style modem only: the AT-command port (e.g. /dev/ttyUSB3) used
# to send AT+QGPS=1 on every startup — the modem's GNSS engine does not stay
# enabled across a power cycle/reboot the way a plain UART GPS module does.
GPS_AT_PORT = None
GPS_AT_BAUD = 115200

# Allow CLI overrides (e.g. passed from connect-to-server.sh)
for arg in sys.argv:
    if arg.startswith("--url="):
        LARAVEL_URL = arg.split("=", 1)[1]
    elif arg.startswith("--token="):
        SURVEILLANCE_TOKEN = arg.split("=", 1)[1]
    elif arg.startswith("--server-name=") or arg.startswith("--jetson-name="):
        SERVER_ID = arg.split("=", 1)[1]
    elif arg.startswith("--interval="):
        HEARTBEAT_INTERVAL = int(arg.split("=", 1)[1])
    elif arg.startswith("--ssh-host="):
        SSH_HOST = arg.split("=", 1)[1]
    elif arg.startswith("--ssh-port="):
        SSH_PORT = int(arg.split("=", 1)[1])
    elif arg.startswith("--ssh-user="):
        SSH_USER = arg.split("=", 1)[1]
    elif arg.startswith("--ssh-key="):
        SSH_KEY_PATH = os.path.expanduser(arg.split("=", 1)[1])
    elif arg.startswith("--gps-port="):
        GPS_PORT = arg.split("=", 1)[1]
    elif arg.startswith("--gps-baud="):
        GPS_BAUD = int(arg.split("=", 1)[1])
    elif arg.startswith("--gps-at-port="):
        GPS_AT_PORT = arg.split("=", 1)[1]
    elif arg.startswith("--gps-at-baud="):
        GPS_AT_BAUD = int(arg.split("=", 1)[1])

GPS_READER = None
if GPS_PORT:
    # Re-enable the modem's GNSS engine on every (re)open, not just once at
    # startup — it turns off whenever the modem resets or is swapped.
    on_open = (lambda: enable_modem_gps(GPS_AT_PORT, GPS_AT_BAUD)) if GPS_AT_PORT else None
    GPS_READER = GpsReader(GPS_PORT, GPS_BAUD, on_open=on_open)
    GPS_READER.start()

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_FILE = "/tmp/device-stats.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [device-stats] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
try:
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [device-stats] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(file_handler)
except Exception as e:
    log.warning(f"Could not initialize logging to {LOG_FILE}: {e}")

START_TIME = time.time()


def read_temperature() -> int:
    """
    Best-effort CPU/SoC temperature in whole degrees Celsius.
    Tries psutil sensors first (works on most boards), then falls back to
    reading /sys/class/thermal directly (Jetson / generic ARM boards).
    """
    try:
        temps = psutil.sensors_temperatures()
        for entries in temps.values():
            for entry in entries:
                if entry.current:
                    return int(round(entry.current))
    except Exception:
        pass

    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            millideg = int(f.read().strip())
            return int(round(millideg / 1000))
    except Exception:
        pass

    return 0


def collect_stats() -> dict:
    cpu = int(round(psutil.cpu_percent(interval=1)))
    ram = int(round(psutil.virtual_memory().percent))
    disk = int(round(psutil.disk_usage("/").percent))
    temperature = read_temperature()
    uptime = int(time.time() - psutil.boot_time())

    stats = {
        "jetson_id": SERVER_ID,
        "hostname": socket.gethostname(),
        "agent_version": "1.0",
        "online": True,
        "uptime": uptime,
        "cpu": cpu,
        "ram": ram,
        "disk": disk,
        "temperature": temperature,
    }

    if GPS_READER is not None:
        lat, lng = GPS_READER.get_position()
        if lat is not None and lng is not None:
            stats["latitude"] = lat
            stats["longitude"] = lng

    return stats


def send_heartbeat(stats: dict) -> bool:
    url = f"{LARAVEL_URL}/api/device-agent/heartbeat"
    headers = {
        "Authorization": f"Bearer {SURVEILLANCE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = requests.post(url, headers=headers, json=stats, timeout=8)
        if r.status_code == 200:
            gps_note = f" gps=({stats['latitude']:.5f},{stats['longitude']:.5f})" if "latitude" in stats else ""
            log.info(
                f"Heartbeat sent: cpu={stats['cpu']}% ram={stats['ram']}% "
                f"disk={stats['disk']}% temp={stats['temperature']}°C{gps_note}"
            )
            return True
        log.warning(f"Heartbeat rejected: HTTP {r.status_code} — {r.text[:200]}")
        return False
    except Exception as e:
        log.warning(f"Heartbeat failed: {e}")
        return False


# ─── Remote Terminal (reverse SSH tunnel) ───────────────────────────────────
# Backs the "Access Terminal" button on the dashboard. The Laravel side
# allocates a session + port and drops an `open_terminal` command in the
# device_agent_commands queue; this agent polls for it, opens a reverse SSH
# tunnel (`ssh -R {port}:localhost:22 device-agent@{LARAVEL host}`) so an
# operator can then `ssh device-agent@{host} -p {port}` into this device
# through the dashboard's server, and reports back so the UI updates.
#
# One-time setup required on the Laravel server for this to actually work:
#   1. A restricted `device-agent` system user (no shell needed, just
#      `AllowTcpForwarding yes` for that user).
#   2. `GatewayPorts clientspecified` (or `yes`) in sshd_config — without
#      this, OpenSSH silently binds the forwarded port to 127.0.0.1 only,
#      so an operator's `ssh ...@host -p {port}` from outside gets
#      "Connection refused" even though the tunnel itself is up.
#   3. Each device's public key (auto-generated below, logged on first use)
#      added to that user's ~/.ssh/authorized_keys, e.g. with
#      `no-pty,no-X11-forwarding,no-agent-forwarding` restrictions.
#   4. Firewall open for the dynamic port range 22000-22999/tcp.
ACTIVE_TERMINAL_SESSIONS = {}  # session_id -> {process, port, expires_at}


def _api_headers() -> dict:
    return {
        "Authorization": f"Bearer {SURVEILLANCE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def report_command_result(command_id: int, status: str, result: str = None) -> None:
    try:
        requests.post(
            f"{LARAVEL_URL}/api/device-agent/command-result",
            headers=_api_headers(),
            json={"command_id": command_id, "status": status, "result": result},
            timeout=8,
        )
    except Exception as e:
        log.warning(f"Failed to report command result for #{command_id}: {e}")


def notify_terminal_ready(session_id: int, port: int) -> bool:
    try:
        r = requests.post(
            f"{LARAVEL_URL}/api/device-agent/terminal-ready",
            headers=_api_headers(),
            json={"session_id": session_id, "port": port},
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Failed to notify terminal-ready for session {session_id}: {e}")
        return False


def notify_terminal_closed(session_id: int, reason: str) -> None:
    try:
        requests.post(
            f"{LARAVEL_URL}/api/device-agent/terminal-closed",
            headers=_api_headers(),
            json={"session_id": session_id, "reason": reason},
            timeout=8,
        )
    except Exception as e:
        log.warning(f"Failed to notify terminal-closed for session {session_id}: {e}")


def ensure_terminal_key() -> bool:
    """Generate this device's dedicated terminal-tunnel keypair on first use.
    Returns True once a usable private key is on disk."""
    if os.path.exists(SSH_KEY_PATH):
        return True
    try:
        os.makedirs(os.path.dirname(SSH_KEY_PATH), exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", SSH_KEY_PATH,
             "-C", f"roadshield-terminal-{SERVER_ID}"],
            check=True, capture_output=True, timeout=15,
        )
        with open(f"{SSH_KEY_PATH}.pub") as f:
            pubkey = f.read().strip()
        log.warning(
            "Generated new terminal SSH key for this device. Add this public "
            f"key to the '{SSH_USER}' user's authorized_keys on {SSH_HOST} "
            f"(restricted, e.g. 'no-pty,no-X11-forwarding,no-agent-forwarding'):\n{pubkey}"
        )
        return False  # key just created — server doesn't trust it yet
    except Exception as e:
        log.error(f"Could not generate terminal SSH key: {e}")
        return False


def handle_open_terminal(command: dict) -> None:
    command_id = command["id"]
    payload = command.get("payload") or {}
    session_id = payload.get("session_id")
    port = payload.get("port")
    timeout_minutes = payload.get("timeout_minutes", 60)

    if not session_id or not port:
        report_command_result(command_id, "failed", "Malformed open_terminal payload")
        return

    if not ensure_terminal_key():
        report_command_result(
            command_id, "failed",
            f"Terminal SSH key not yet authorized on {SSH_HOST} — see agent log for the public key to add.",
        )
        notify_terminal_closed(session_id, "ssh_key_not_authorized")
        return

    report_command_result(command_id, "executing")

    ssh_cmd = [
        "ssh", "-N",
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-i", SSH_KEY_PATH,
        "-p", str(SSH_PORT),
        "-R", f"0.0.0.0:{port}:localhost:22",
        f"{SSH_USER}@{SSH_HOST}",
    ]

    try:
        process = subprocess.Popen(ssh_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception as e:
        report_command_result(command_id, "failed", f"Failed to start ssh: {e}")
        notify_terminal_closed(session_id, "spawn_failed")
        return

    time.sleep(3)  # give the tunnel a moment to fail fast on auth/network errors
    if process.poll() is not None:
        stderr = process.stderr.read().decode(errors="ignore")[:300] if process.stderr else ""
        report_command_result(command_id, "failed", f"SSH tunnel exited immediately: {stderr}")
        notify_terminal_closed(session_id, "tunnel_failed")
        return

    ACTIVE_TERMINAL_SESSIONS[session_id] = {
        "process": process,
        "port": port,
        "expires_at": time.time() + timeout_minutes * 60,
    }

    if notify_terminal_ready(session_id, port):
        report_command_result(command_id, "completed", f"Tunnel open on port {port}")
        log.info(f"Terminal session {session_id} open on port {port} (expires in {timeout_minutes}m)")
    else:
        report_command_result(command_id, "failed", "Tunnel opened but dashboard rejected terminal-ready")
        process.terminate()
        ACTIVE_TERMINAL_SESSIONS.pop(session_id, None)


COMMAND_HANDLERS = {
    "open_terminal": handle_open_terminal,
}


def poll_pending_commands() -> None:
    try:
        r = requests.get(
            f"{LARAVEL_URL}/api/device-agent/pending-commands",
            headers=_api_headers(),
            params={"jetson_id": SERVER_ID},
            timeout=8,
        )
        if r.status_code != 200:
            return
        commands = r.json().get("commands", [])
    except Exception as e:
        log.warning(f"Failed to poll pending commands: {e}")
        return

    for command in commands:
        handler = COMMAND_HANDLERS.get(command.get("command"))
        if handler is None:
            report_command_result(command["id"], "failed", f"Unknown command: {command.get('command')}")
            continue
        try:
            handler(command)
        except Exception as e:
            log.error(f"Error handling command #{command['id']}: {e}")
            report_command_result(command["id"], "failed", str(e))


def monitor_terminal_sessions() -> None:
    now = time.time()
    for session_id, session in list(ACTIVE_TERMINAL_SESSIONS.items()):
        process = session["process"]

        if now >= session["expires_at"]:
            log.info(f"Terminal session {session_id} timed out — closing tunnel")
            process.terminate()
            notify_terminal_closed(session_id, "timeout")
            ACTIVE_TERMINAL_SESSIONS.pop(session_id, None)
            continue

        if process.poll() is not None:
            stderr = process.stderr.read().decode(errors="ignore")[:300] if process.stderr else ""
            log.warning(f"Terminal session {session_id} tunnel dropped unexpectedly: {stderr}")
            notify_terminal_closed(session_id, "connection_lost")
            ACTIVE_TERMINAL_SESSIONS.pop(session_id, None)


def _cleanup_terminal_sessions(*_args) -> None:
    for session in ACTIVE_TERMINAL_SESSIONS.values():
        session["process"].terminate()


signal.signal(signal.SIGTERM, lambda *_: (_cleanup_terminal_sessions(), sys.exit(0)))


def main():
    log.info("=" * 60)
    log.info("RoadShield Device Stats Agent")
    log.info(f"  Server : {LARAVEL_URL}")
    log.info(f"  Device : {SERVER_ID}")
    log.info(f"  Every  : {HEARTBEAT_INTERVAL}s")
    log.info("=" * 60)

    while True:
        try:
            stats = collect_stats()
            send_heartbeat(stats)
        except Exception as e:
            log.error(f"Unexpected error collecting/sending stats: {e}")

        try:
            poll_pending_commands()
            monitor_terminal_sessions()
        except Exception as e:
            log.error(f"Unexpected error in terminal command handling: {e}")

        time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        _cleanup_terminal_sessions()
