#!/usr/bin/env python3
"""
RoadShield — GPS Service for Jetson
=====================================
Reads NMEA sentences from /dev/ttyTHS1 (Jetson UART), parses $GNGGA
sentences, converts to decimal lat/lng, and sends GPS updates via
the JetsonWSClient WebSocket connection.

Usage:
    # Standalone:
    python3 gps_service.py

    # Or import and integrate:
    from gps_service import GpsReader
    gps = GpsReader("/dev/ttyTHS1", 9600)
    gps.start()
    lat, lng = gps.get_position()
"""

import glob
import serial
import subprocess
import time
import threading
import logging
import json
import os

log = logging.getLogger("gps-service")


def _enable_via_modemmanager(retries: int, delay: float) -> bool:
    """
    Ask ModemManager to turn on the modem's GNSS engine via its Location API
    (mmcli --location-enable-gps-unmanaged).

    Once ModemManager owns the modem (needed for it to manage the cellular
    data connection), it holds the AT port open exclusively, so a raw
    AT+QGPS=1 write from here would just fail with "port busy" — mmcli is
    ModemManager's own control channel for the same command and doesn't
    compete for the port.

    "unmanaged" (not gps-nmea/gps-raw) is required: it starts the engine but
    leaves the NMEA port closed so GpsReader can open it. With gps-nmea,
    ModemManager opens the NMEA port itself and GpsReader gets "port busy".

    Needs polkit permission for org.freedesktop.ModemManager1.* when not run
    as root — installed by setup-modem-gps.sh.
    """
    try:
        modem_list = subprocess.run(
            ["mmcli", "-L"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception as e:
        log.debug("mmcli not available: %s", e)
        return False

    if "/Modem/" not in modem_list:
        return False
    modem_path = modem_list.split("/Modem/", 1)[1].split()[0]

    for attempt in range(1, retries + 1):
        try:
            result = subprocess.run(
                ["mmcli", "-m", modem_path, "--location-enable-gps-unmanaged"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                log.info("Modem GPS engine enabled via ModemManager (modem %s)", modem_path)
                return True
            log.warning("mmcli GPS enable attempt %d: %s", attempt, result.stderr.strip())
        except Exception as e:
            log.warning("mmcli GPS enable attempt %d failed: %s", attempt, e)
        time.sleep(delay)

    return False


def enable_modem_gps(at_port: str, baudrate: int = 115200, retries: int = 3) -> bool:
    """
    Turn on the GNSS engine on a Quectel-style modem (e.g. EC25) so NMEA
    starts flowing on its dedicated GPS port. This state is NOT saved across
    a modem power cycle/reboot, so it must be re-sent every time the agent
    starts — unlike a UART GPS module (Jetson's /dev/ttyTHS1), which just
    streams NMEA on its own with no activation step.

    Tries ModemManager's Location API first (mmcli) since ModemManager holds
    the AT port exclusively whenever it's managing the modem for the cellular
    data connection — a raw AT+QGPS=1 write would just fail as "port busy" in
    that case. Falls back to writing AT+QGPS=1 directly for setups where
    ModemManager isn't managing this modem (or isn't installed).
    Returns True if GPS was enabled (or was already on).
    """
    if _enable_via_modemmanager(retries, delay=1):
        return True

    if glob.has_magic(at_port):
        matches = sorted(glob.glob(at_port))
        at_port = matches[0] if matches else at_port

    for attempt in range(1, retries + 1):
        try:
            with serial.Serial(at_port, baudrate, timeout=2) as ser:
                ser.write(b'AT+QGPS=1\r\n')
                time.sleep(0.5)
                response = ser.read(200).decode(errors='ignore')
                if 'OK' in response or 'already' in response.lower():
                    log.info("Modem GPS engine enabled on %s", at_port)
                    return True
                if '+CME ERROR' in response:
                    log.warning("Modem GPS enable attempt %d got: %s", attempt, response.strip())
        except Exception as e:
            log.warning("Modem GPS enable attempt %d failed: %s", attempt, e)
        time.sleep(1)

    log.error("Could not enable modem GPS engine on %s after %d attempts", at_port, retries)
    return False


# ── NMEA Parser ─────────────────────────────────────────────────────────────


def nmea_to_decimal(raw_value: str, direction: str) -> float | None:
    """
    Convert NMEA coordinate (ddmm.mmmm or dddmm.mmmm) to decimal degrees.

    Examples:
        nmea_to_decimal("2512.1234", "N")  → 25.202057
        nmea_to_decimal("05516.5678", "E") → 55.276130
    """
    if not raw_value or not direction:
        return None

    try:
        raw = float(raw_value)
    except ValueError:
        return None

    # Determine degrees / minutes split
    if direction in ('N', 'S'):
        degrees = int(raw / 100)
        minutes = raw - (degrees * 100)
    else:  # E, W
        degrees = int(raw / 100)
        minutes = raw - (degrees * 100)

    decimal = degrees + (minutes / 60.0)

    if direction in ('S', 'W'):
        decimal = -decimal

    return round(decimal, 7)


def parse_gngga(sentence: str) -> dict | None:
    """
    Parse a $GNGGA (or $GPGGA) NMEA sentence.

    Returns dict with: latitude, longitude, altitude, fix_quality, satellites
    or None if the sentence is invalid / no fix.

    Example sentence:
        $GNGGA,123519,2512.1234,N,05516.5678,E,1,08,0.9,545.4,M,46.9,M,,*47
    """
    if not sentence:
        return None

    # Strip checksum
    if '*' in sentence:
        sentence = sentence.split('*')[0]

    parts = sentence.split(',')

    # Validate: must be GGA with enough fields
    if len(parts) < 10:
        return None

    msg_type = parts[0]
    if msg_type not in ('$GNGGA', '$GPGGA', '$GLGGA'):
        return None

    # Fix quality: 0 = no fix
    try:
        fix_quality = int(parts[6]) if parts[6] else 0
    except ValueError:
        fix_quality = 0

    if fix_quality == 0:
        return None

    lat = nmea_to_decimal(parts[2], parts[3])
    lng = nmea_to_decimal(parts[4], parts[5])

    if lat is None or lng is None:
        return None

    # Satellites
    try:
        satellites = int(parts[7]) if parts[7] else 0
    except ValueError:
        satellites = 0

    # Altitude
    try:
        altitude = float(parts[9]) if parts[9] else None
    except ValueError:
        altitude = None

    return {
        'latitude': lat,
        'longitude': lng,
        'altitude': altitude,
        'fix_quality': fix_quality,
        'satellites': satellites,
        'utc_time': parts[1] if parts[1] else None,
    }


# ── GPS Reader Thread ────────────────────────────────────────────────────────


class GpsReader(threading.Thread):
    """
    Background thread that continuously reads NMEA data from a serial port
    and keeps the latest GPS position updated.
    """

    def __init__(self, port: str = "/dev/ttyTHS1", baudrate: int = 9600, on_open=None):
        """
        port may be a glob (e.g. /dev/serial/by-id/usb-Quectel_*-if01-port0);
        it is re-resolved on every (re)open so a swapped modem with a different
        serial number is picked up without restarting.
        on_open, if given, is called before each (re)open — used to re-enable
        a modem's GNSS engine, which turns off whenever the modem resets.
        """
        super().__init__()
        self.daemon = True
        self.port = port
        self.baudrate = baudrate
        self.on_open = on_open
        self.running = False

        self._latitude = None
        self._longitude = None
        self._altitude = None
        self._speed = None
        self._satellites = 0
        self._fix_quality = 0
        self._last_update = 0
        self._lock = threading.Lock()

    def run(self):
        """Main loop: open serial and read NMEA sentences continuously."""
        self.running = True
        log.info("GPS reader starting on %s @ %d baud", self.port, self.baudrate)

        while self.running:
            try:
                self._read_serial()
            except Exception as e:
                log.error("GPS serial error: %s — retrying in 5s", e)
                time.sleep(5)

        log.info("GPS reader stopped.")

    def _read_serial(self):
        """Open serial port and continuously read lines."""
        port = self.port
        if glob.has_magic(port):
            matches = sorted(glob.glob(port))
            if not matches:
                raise FileNotFoundError(f"no device matches {port}")
            port = matches[0]

        if self.on_open:
            self.on_open()

        with serial.Serial(
            port,
            self.baudrate,
            timeout=2,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        ) as ser:
            log.info("GPS serial port opened: %s", port)

            while self.running:
                try:
                    line = ser.readline().decode('ascii', errors='ignore').strip()
                except Exception:
                    continue

                if not line:
                    continue

                # Parse GGA sentences
                if line.startswith('$GNGGA') or line.startswith('$GPGGA'):
                    result = parse_gngga(line)
                    if result:
                        with self._lock:
                            self._latitude = result['latitude']
                            self._longitude = result['longitude']
                            self._altitude = result.get('altitude')
                            self._satellites = result.get('satellites', 0)
                            self._fix_quality = result.get('fix_quality', 0)
                            self._last_update = time.time()

                        log.debug(
                            "GPS fix: %.7f, %.7f (alt=%.1f, sats=%d)",
                            result['latitude'],
                            result['longitude'],
                            result.get('altitude') or 0,
                            result.get('satellites', 0),
                        )

                # Parse RMC for speed (optional)
                if line.startswith('$GNRMC') or line.startswith('$GPRMC'):
                    self._parse_rmc_speed(line)

    def _parse_rmc_speed(self, sentence: str):
        """Extract speed from RMC sentence (field 7 = speed in knots)."""
        try:
            if '*' in sentence:
                sentence = sentence.split('*')[0]
            parts = sentence.split(',')
            if len(parts) > 7 and parts[7]:
                speed_knots = float(parts[7])
                speed_kmh = speed_knots * 1.852
                with self._lock:
                    self._speed = round(speed_kmh, 1)
        except (ValueError, IndexError):
            pass

    def stop(self):
        self.running = False

    def get_position(self) -> tuple[float | None, float | None]:
        """Return (latitude, longitude) or (None, None) if no fix."""
        with self._lock:
            return self._latitude, self._longitude

    def get_full_data(self) -> dict:
        """Return all GPS data as a dictionary."""
        with self._lock:
            return {
                'latitude': self._latitude,
                'longitude': self._longitude,
                'altitude': self._altitude,
                'speed': self._speed,
                'satellites': self._satellites,
                'fix_quality': self._fix_quality,
                'last_update': self._last_update,
                'has_fix': self._latitude is not None and self._longitude is not None,
            }


# ── GPS Sender (WebSocket integration) ───────────────────────────────────────


class GpsSender(threading.Thread):
    """
    Periodically sends GPS data to the server via the JetsonWSClient.
    This runs as a separate thread and calls ws_client.send_sync().
    """

    def __init__(
        self,
        gps_reader: GpsReader,
        ws_client,
        interval: float = 5.0,
    ):
        super().__init__()
        self.daemon = True
        self.gps_reader = gps_reader
        self.ws_client = ws_client
        self.interval = interval
        self.running = False

    def run(self):
        self.running = True
        log.info("GPS sender started (every %.1fs)", self.interval)

        while self.running:
            try:
                data = self.gps_reader.get_full_data()
                if data['has_fix']:
                    self.ws_client.send_sync("gps.update", {
                        'latitude': data['latitude'],
                        'longitude': data['longitude'],
                        'altitude': data['altitude'],
                        'speed': data['speed'],
                        'satellites': data['satellites'],
                        'timestamp': time.time(),
                    })
                    log.debug(
                        "GPS sent: %.7f, %.7f",
                        data['latitude'], data['longitude']
                    )
            except Exception as e:
                log.warning("GPS send error: %s", e)

            time.sleep(self.interval)

    def stop(self):
        self.running = False


# ── Standalone mode ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyTHS1"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 9600

    print(f"Starting GPS reader on {port} @ {baud} baud")
    print("Press Ctrl+C to stop.\n")

    reader = GpsReader(port, baud)
    reader.start()

    try:
        while True:
            data = reader.get_full_data()
            if data['has_fix']:
                print(
                    f"  Lat: {data['latitude']:.7f}  "
                    f"Lng: {data['longitude']:.7f}  "
                    f"Alt: {data['altitude']}m  "
                    f"Speed: {data['speed']} km/h  "
                    f"Sats: {data['satellites']}"
                )
            else:
                print("  Waiting for GPS fix...")
            time.sleep(2)
    except KeyboardInterrupt:
        reader.stop()
        print("\nStopped.")
