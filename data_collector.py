#!/usr/bin/env python3
"""
RoadShield — data_collector.py
==============================
Collects training images on the car itself, for the work-zone model
(cones, workers, lanes, the taper, intruding vehicles). Nothing is uploaded:
images stay on the local NVMe under dataset/ until they are moved off the
device.

Per TandemVu camera (one IP, two lenses):
  • Fixed (wide) lens  — a snapshot every FIXED_INTERVAL seconds, skipped when
    the scene clearly hasn't changed (parked car).
  • PTZ (zoom) lens    — every SWEEP_INTERVAL seconds a sweep over a grid of
    pan offsets × zoom levels around where the PTZ currently points, one
    snapshot per position, then the PTZ goes back to where it was.

Snapshots come straight from the camera (ISAPI .../picture), so no video is
decoded here and recording/streaming are untouched.

Channel map, verified on a DS-2SE4C425MWG-E/14 by zooming and comparing:
streaming channel 1 (101) is the PTZ/zoom lens, channel 2 (201) is the fixed
wide lens (the one that always shows the TMA's own attenuator).

If someone moves the PTZ from the dashboard, the sweep backs off for
OPERATOR_BACKOFF seconds so it never fights an operator.

Settings can be overridden in ~/.roadshield/collector.json, e.g.
    {"enabled": false}            stop collecting on this car
    {"sweep_interval": 600}       sweep every 10 min
"""
import datetime as dt
import json
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time

import requests
from requests.auth import HTTPDigestAuth

from device_identity import get_device_id

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = os.path.join(SCRIPT_DIR, "dataset")
CAMERAS_JSON = os.path.join(SCRIPT_DIR, "cameras_config.json")
SETTINGS_JSON = os.path.expanduser("~/.roadshield/collector.json")

DEFAULTS = {
    "enabled": True,
    "fixed_interval": 5,          # s between fixed-lens snapshots
    "fixed_keep_every": 300,      # s — keep one even if the scene looks unchanged
    "sweep_interval": 300,        # s between PTZ sweeps
    "pan_offsets": [-40, -20, 0, 20, 40],   # degrees around the current heading
    "zoom_levels": [1, 4, 12],    # optical zoom factors
    "settle": 3.0,                # s to wait after a move (motion + autofocus)
    "operator_backoff": 900,      # s to pause sweeps after a manual PTZ move
    "min_free_gb": 50,            # stop writing below this much free disk
    "max_per_day": 6000,          # per camera per lens
}
TIMEOUT = 8
FIXED_CHANNEL = 201
PTZ_CHANNEL = 101

logging.basicConfig(level=logging.INFO, format="%(asctime)s [collector] %(levelname)s: %(message)s")
log = logging.getLogger("collector")
running = True


def settings() -> dict:
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_JSON) as f:
            s.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Ignoring bad %s: %s", SETTINGS_JSON, e)
    return s


def load_cameras() -> list:
    """One entry per physical camera IP (the dashboard lists each lens as its
    own camera, with roles that are not always consistent)."""
    try:
        with open(CAMERAS_JSON) as f:
            cfg = json.load(f)
    except Exception as e:
        log.error("Cannot read %s: %s", CAMERAS_JSON, e)
        return []
    cams = {}
    for key, c in cfg.items():
        if c.get("type", "hikvision") != "hikvision" or not c.get("ip"):
            continue
        cam = cams.setdefault(c["ip"], {"ip": c["ip"], "user": c["user"], "password": c["password"],
                                        "keys": [], "has_ptz": False})
        cam["keys"].append(key)
        cam["has_ptz"] = cam["has_ptz"] or bool(c.get("ptz"))
    return list(cams.values())


def free_gb() -> float:
    return shutil.disk_usage(SCRIPT_DIR).free / 1e9


class Camera:
    def __init__(self, c: dict, device: str):
        self.ip = c["ip"]
        self.auth = HTTPDigestAuth(c["user"], c["password"])
        self.device = device
        self.has_ptz = c["has_ptz"]
        self.name = c["ip"].replace(".", "_")
        self.counts = {}            # (date, lens) -> saved today
        self.last_size = {}         # lens -> size of last kept jpeg
        self.last_kept = {}         # lens -> time of last kept jpeg
        self.expected_pos = None    # where the sweep left the PTZ
        self.backoff_until = 0.0

    # ── ISAPI ──
    def snapshot(self, channel: int) -> bytes | None:
        try:
            r = requests.get(f"http://{self.ip}/ISAPI/Streaming/channels/{channel}/picture",
                             auth=self.auth, timeout=TIMEOUT)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                return r.content
            log.debug("%s ch%s snapshot HTTP %s", self.ip, channel, r.status_code)
        except requests.RequestException as e:
            log.debug("%s ch%s snapshot failed: %s", self.ip, channel, e)
        return None

    def ptz_status(self) -> dict | None:
        try:
            r = requests.get(f"http://{self.ip}/ISAPI/PTZCtrl/channels/1/status", auth=self.auth, timeout=TIMEOUT)
            vals = {k: re.search(rf"<{k}>(-?\d+)</{k}>", r.text) for k in ("elevation", "azimuth", "absoluteZoom")}
            if r.status_code == 200 and all(vals.values()):
                return {k: int(v.group(1)) for k, v in vals.items()}
        except requests.RequestException:
            pass
        return None

    def ptz_goto(self, azimuth: int, elevation: int, zoom: int) -> bool:
        xml = (f"<PTZData><AbsoluteHigh><elevation>{elevation}</elevation><azimuth>{azimuth % 3600}</azimuth>"
               f"<absoluteZoom>{max(10, min(250, zoom))}</absoluteZoom></AbsoluteHigh></PTZData>")
        try:
            r = requests.put(f"http://{self.ip}/ISAPI/PTZCtrl/channels/1/absolute", data=xml.encode(),
                             headers={"Content-Type": "application/xml"}, auth=self.auth, timeout=TIMEOUT)
            return r.status_code in (200, 204)
        except requests.RequestException:
            return False

    # ── storage ──
    def save(self, lens: str, jpeg: bytes, meta: dict, cfg: dict) -> bool:
        now = dt.datetime.now()
        day = now.strftime("%Y-%m-%d")
        key = (day, lens)
        if self.counts.get(key, 0) >= cfg["max_per_day"]:
            return False
        if free_gb() < cfg["min_free_gb"]:
            log.warning("Free disk below %s GB — not saving", cfg["min_free_gb"])
            return False
        folder = os.path.join(DATASET_DIR, self.device, self.name, lens, day)
        os.makedirs(folder, exist_ok=True)
        suffix = "".join(f"_{k[0]}{v}" for k, v in meta.get("ptz", {}).items()) if lens == "ptz" else ""
        fname = now.strftime("%H%M%S_%f")[:-3] + suffix + ".jpg"
        with open(os.path.join(folder, fname), "wb") as f:
            f.write(jpeg)
        record = {"file": fname, "time": now.isoformat(timespec="milliseconds"), "device": self.device,
                  "camera_ip": self.ip, "lens": lens, "bytes": len(jpeg), **meta}
        with open(os.path.join(folder, "index.jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")
        self.counts[key] = self.counts.get(key, 0) + 1
        return True

    # ── fixed lens ──
    def fixed_tick(self, cfg: dict):
        jpeg = self.snapshot(FIXED_CHANNEL)
        if not jpeg:
            return
        now = time.time()
        last = self.last_size.get("fixed")
        # Same scene → near-identical JPEG size. Cheap stand-in for an image
        # diff (no OpenCV on the cars); proper dedup happens off-device.
        unchanged = last and abs(len(jpeg) - last) / last < 0.015
        if unchanged and now - self.last_kept.get("fixed", 0) < cfg["fixed_keep_every"]:
            return
        if self.save("fixed", jpeg, {}, cfg):
            self.last_size["fixed"] = len(jpeg)
            self.last_kept["fixed"] = now

    # ── PTZ sweep ──
    def sweep(self, cfg: dict):
        start = self.ptz_status()
        if not start:
            return
        if self.expected_pos and start != self.expected_pos:
            log.info("%s PTZ was moved by someone else — pausing sweeps for %ds", self.ip, cfg["operator_backoff"])
            self.backoff_until = time.time() + cfg["operator_backoff"]
            self.expected_pos = None
            return
        n = 0
        try:
            for zoom in cfg["zoom_levels"]:
                for pan in cfg["pan_offsets"]:
                    if not running:
                        return
                    az, el, z = start["azimuth"] + pan * 10, start["elevation"], int(zoom * 10)
                    if not self.ptz_goto(az, el, z):
                        continue
                    time.sleep(cfg["settle"])
                    jpeg = self.snapshot(PTZ_CHANNEL)
                    if jpeg and self.save("ptz", jpeg, {"ptz": {"azimuth": az % 3600, "elevation": el, "zoom": z}}, cfg):
                        n += 1
        finally:
            self.ptz_goto(start["azimuth"], start["elevation"], start["absoluteZoom"])
            time.sleep(cfg["settle"])
            self.expected_pos = self.ptz_status()
        log.info("%s PTZ sweep: %d images", self.ip, n)

    def run(self):
        next_fixed = next_sweep = 0.0
        while running:
            cfg = settings()
            if not cfg["enabled"]:
                time.sleep(30)
                continue
            now = time.time()
            if now >= next_fixed:
                next_fixed = now + cfg["fixed_interval"]
                self.fixed_tick(cfg)
            if self.has_ptz and now >= next_sweep and now >= self.backoff_until:
                next_sweep = now + cfg["sweep_interval"]
                self.sweep(cfg)
            time.sleep(0.5)


def main():
    device = get_device_id()
    cams = load_cameras()
    if not cams:
        log.error("No Hikvision cameras configured — nothing to collect")
        return
    log.info("Collecting for %s into %s: %s", device, DATASET_DIR,
             ", ".join(f"{c['ip']} ({'+'.join(c['keys'])}{', PTZ' if c['has_ptz'] else ''})" for c in cams))
    threads = [threading.Thread(target=Camera(c, device).run, daemon=True) for c in cams]
    for t in threads:
        t.start()

    def stop(*_):
        global running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running and any(t.is_alive() for t in threads):
        time.sleep(1)
    # give a sweep in progress the chance to put the PTZ back
    for t in threads:
        t.join(timeout=15)


if __name__ == "__main__":
    sys.exit(main())
