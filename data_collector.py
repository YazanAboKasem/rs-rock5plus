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
  • PTZ (zoom) lens    — every sweep_interval seconds a sweep aimed at the
    road: a 1x shot, the road's vanishing point found in it (lane lines
    converging, see road_geometry.py), the PTZ turned onto that point and
    re-checked at 3x, then 6x/12x/25x shots down the road with offsets that
    shrink as the field of view narrows; then the PTZ goes back where it
    was. The far end of the road is where a taper, distant cones and
    approaching traffic are, and the frame centre often is not on the road.
    Without OpenCV, or when no vanishing point is found, only the 1x/3x
    shots are taken (higher zoom on an unknown target mostly frames trees).

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
import importlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import requests
from requests.auth import HTTPDigestAuth

from device_identity import get_device_id

# OpenCV + numpy for road_geometry.py live in a project-local folder
# (pip --target, ~55 MB of wheels instead of ~150 MB of apt packages over
# cellular, no sudo); installed on first run by ensure_image_libs().
PYLIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pylib")
sys.path.insert(0, PYLIB)
vanishing_point_jpeg = None


def load_image_libs() -> bool:
    global vanishing_point_jpeg
    if vanishing_point_jpeg is None:
        # .pylib may not have existed when Python first scanned sys.path, and
        # the path importer cache would keep reporting it empty after install.
        sys.path_importer_cache.pop(PYLIB, None)
        importlib.invalidate_caches()
        try:
            from road_geometry import vanishing_point_jpeg as vp
            vanishing_point_jpeg = vp
        except Exception:
            return False
    return True


def ensure_image_libs():
    """Background: pip-install OpenCV/numpy into .pylib if missing."""
    if load_image_libs():
        return
    log.info("Installing opencv-python-headless + numpy into %s (one-time download)", PYLIB)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", PYLIB,
                        "--break-system-packages", "--no-warn-script-location",
                        "opencv-python-headless", "numpy"], capture_output=True, text=True, timeout=1800)
    if r.returncode == 0 and load_image_libs():
        log.info("Image libraries ready — sweeps now aim at the road")
    else:
        log.warning("Image library install failed (%s) — sweeps stay at 1x/3x", (r.stderr or r.stdout).strip()[-200:])

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = os.path.join(SCRIPT_DIR, "dataset")
CAMERAS_JSON = os.path.join(SCRIPT_DIR, "cameras_config.json")
SETTINGS_JSON = os.path.expanduser("~/.roadshield/collector.json")
# camera-control.py logs every PTZ command it receives from the dashboard;
# that is the reliable sign an operator is using the PTZ right now.
CAMERA_CONTROL_LOG = "/tmp/camera-control.log"

DEFAULTS = {
    "enabled": True,
    "fixed_interval": 5,          # s between fixed-lens snapshots
    "fixed_keep_every": 300,      # s — keep one even if the scene looks unchanged
    "sweep_interval": 180,        # s between PTZ sweeps
    # [optical zoom, [pan offsets °], [tilt offsets °, + = down]] around the
    # road's vanishing point. The field of view narrows with zoom (~58° at
    # 1x, ~2.4° at 25x) so offsets shrink with it. At 12x/25x a frame centred
    # exactly on the vanishing point is half sky and lamp posts; tilted
    # slightly down it fills with the road short of the far end — cones,
    # arrow boards and approaching cars (checked on a live work zone).
    "sweep_plan": [[3, [-6, 0, 6], [0]], [6, [-3, 0, 3], [0.6]],
                   [12, [-1.5, 0, 1.5], [0.4, 1.3]], [25, [-0.7, 0, 0.7], [0.25, 0.8]]],
    "hfov_1x": 57.6,              # horizontal field of view at 1x (degrees)
    "vp_min_conf": 0.45,          # vanishing-point confidence needed to zoom past 3x
    "settle": 3.0,                # s to wait after a move (motion + autofocus)
    "settle_long_zoom": 5.0,      # s at 12x and above (longer focus travel)
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


def last_operator_ptz() -> float:
    """Epoch time of the latest dashboard PTZ command, 0 if none seen."""
    try:
        with open(CAMERA_CONTROL_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode(errors="ignore").splitlines()
    except OSError:
        return 0.0
    for line in reversed(tail):
        if "ptz.command" in line:
            try:
                return dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                continue
    return 0.0


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
        self.sweep_started = 0.0

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
    def ptz_settled(self, timeout=20.0) -> dict | None:
        """Status once two reads 1s apart agree (a zoom from 25x to 1x takes
        several seconds; reading mid-move would look like an operator)."""
        last, t_end = None, time.time() + timeout
        while time.time() < t_end:
            cur = self.ptz_status()
            if cur and cur == last:
                return cur
            last = cur
            time.sleep(1.0)
        return last

    @staticmethod
    def same_pos(a: dict, b: dict) -> bool:
        d_az = abs((a["azimuth"] - b["azimuth"] + 1800) % 3600 - 1800)
        return d_az <= 5 and abs(a["elevation"] - b["elevation"]) <= 5 and abs(a["absoluteZoom"] - b["absoluteZoom"]) <= 3

    @staticmethod
    def frame_to_angles(x: float, y: float, zoom: float, cfg: dict, aspect: float = 1440 / 2560):
        """Offset (pan°, tilt° + = down) of image point (x, y) from the centre."""
        th = math.tan(math.radians(cfg["hfov_1x"] / 2)) / zoom
        tv = th * aspect
        return (math.degrees(math.atan((x - 0.5) * 2 * th)),
                math.degrees(math.atan((y - 0.5) * 2 * tv)))

    class OperatorTookOver(Exception):
        pass

    def shot(self, az: int, el: int, zoom: float, cfg: dict, extra: dict | None = None):
        if last_operator_ptz() >= self.sweep_started:
            raise self.OperatorTookOver()
        z = int(round(zoom * 10))
        if not self.ptz_goto(az, el, z):
            return None
        time.sleep(cfg["settle_long_zoom"] if zoom >= 12 else cfg["settle"])
        jpeg = self.snapshot(PTZ_CHANNEL)
        if not jpeg:
            return None
        vp = vanishing_point_jpeg(jpeg) if zoom <= 3 and load_image_libs() else None
        meta = {"ptz": {"azimuth": az % 3600, "elevation": el, "zoom": z}}
        if vp:
            meta["vanishing_point"] = {"x": round(vp[0], 4), "y": round(vp[1], 4), "conf": round(vp[2], 2)}
        meta.update(extra or {})
        saved = self.save("ptz", jpeg, meta, cfg)
        return vp, saved

    def sweep(self, cfg: dict):
        start = self.ptz_settled(timeout=5)
        if not start:
            return
        if self.expected_pos and not self.same_pos(start, self.expected_pos):
            log.info("%s PTZ was moved by someone else (%s → %s) — pausing sweeps for %ds",
                     self.ip, self.expected_pos, start, cfg["operator_backoff"])
            self.backoff_until = time.time() + cfg["operator_backoff"]
            self.expected_pos = None
            return
        n, aimed, restore = 0, False, True
        self.sweep_started = time.time() - 1
        try:
            az0, el0 = start["azimuth"], start["elevation"]
            # 1x from the current heading, then aim at the road's far end
            r = self.shot(az0, el0, 1, cfg, {"step": "overview"})
            n += bool(r and r[1])
            vp = r[0] if r else None
            if vp and vp[2] >= cfg["vp_min_conf"]:
                pan, tilt = self.frame_to_angles(vp[0], vp[1], 1, cfg)
                az0, el0 = az0 + round(pan * 10), el0 + round(tilt * 10)
                # re-check at 3x: corrects the FOV/lens model error up close
                r = self.shot(az0, el0, 3, cfg, {"step": "aim"})
                n += bool(r and r[1])
                if r and r[0] and r[0][2] >= cfg["vp_min_conf"]:
                    pan, tilt = self.frame_to_angles(r[0][0], r[0][1], 3, cfg)
                    az0, el0 = az0 + round(pan * 10), el0 + round(tilt * 10)
                    aimed = True
            for zoom, pans, tilts in cfg["sweep_plan"]:
                if zoom > 3 and not aimed:
                    break
                for tilt in tilts:
                    for pan in pans:
                        if not running:
                            return
                        r = self.shot(az0 + round(pan * 10), el0 + round(tilt * 10), zoom, cfg,
                                      {"step": "road", "aimed": aimed})
                        n += bool(r and r[1])
        except self.OperatorTookOver:
            # leave the PTZ where the operator is putting it
            restore = False
            self.expected_pos = None
            self.backoff_until = time.time() + cfg["operator_backoff"]
            log.info("%s operator used the PTZ mid-sweep — stopped after %d images, paused %ds",
                     self.ip, n, cfg["operator_backoff"])
            return
        finally:
            if restore:
                self.ptz_goto(start["azimuth"], start["elevation"], start["absoluteZoom"])
                self.expected_pos = self.ptz_settled() or start
        log.info("%s PTZ sweep: %d images (%s)", self.ip, n, "aimed at the road" if aimed else "no vanishing point — 1x/3x only")

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
            if now - last_operator_ptz() < cfg["operator_backoff"]:
                self.expected_pos = None   # operator moved it; start fresh later
            elif self.has_ptz and now >= next_sweep and now >= self.backoff_until:
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
    threading.Thread(target=ensure_image_libs, daemon=True).start()
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
