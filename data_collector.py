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
    road. The fixed lens always looks down the road behind the TMA, so its
    vanishing point (lane lines converging, road_geometry.py), checked every
    20 s and taken as the median of the last 10 min, is the target; the
    zoom lens's 1x frame is matched to the fixed frame (SIFT, OSD masked)
    to turn that point into PTZ angles. Fallback: the vanishing point in
    the zoom lens's own 1x frame, refined at 3x. Then 6x/12x/25x shots down
    the road with offsets that shrink as the field of view narrows, and the
    PTZ goes back where it was.
    When a line of traffic cones is in view (colour-based) the sweep also
    zooms on the work zone: 4x on the middle of the cones, 8x on the
    farthest ones — the start of the taper.
    Frames with no detail and no cone colour (sky, walls, ceilings, noise at
    long zoom at night) are not saved and stop the zoom ladder.
  • Camera clocks are set from the car's clock when they drift > 10 s.
  • Files are written temp → fsync → rename: the cars lose power abruptly.

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
RG = None          # road_geometry, once OpenCV/numpy import
np = cv2 = None


def load_image_libs() -> bool:
    global RG, np, cv2
    if RG is None:
        # .pylib may not have existed when Python first scanned sys.path, and
        # the path importer cache would keep reporting it empty after install.
        sys.path_importer_cache.pop(PYLIB, None)
        importlib.invalidate_caches()
        try:
            import road_geometry as rg
            RG, np, cv2 = rg, rg.np, rg.cv2
        except Exception:
            return False
    return True


def decode(jpeg: bytes):
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


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
    "idle_sweep_interval": 900,   # s between sweeps after idle_after sweeps found no road
    "idle_after": 3,              #   (parked in a yard, facing a wall, ...)
    "fixed_diff": 5.0,            # mean abs diff (0..255) of 64x36 thumbnails that counts as a new scene
    # [optical zoom, [pan offsets °], [tilt offsets °, + = down]] around the
    # road's vanishing point. The field of view narrows with zoom (~58° at
    # 1x, ~2.4° at 25x) so offsets shrink with it. At 12x/25x a frame centred
    # exactly on the vanishing point is half sky and lamp posts; tilted
    # slightly down it fills with the road short of the far end — cones,
    # arrow boards and approaching cars (checked on a live work zone).
    "sweep_plan": [[3, [-6, 0, 6], [0]], [6, [-3, 0, 3], [0.6]],
                   [12, [-1.5, 0, 1.5], [0.4, 1.3]], [25, [-0.7, 0, 0.7], [0.25, 0.8]]],
    # Measured on q8b7 by panning/tilting a parked camera and phase-
    # correlating frames: 55.1° x 31.5° at 1x, and the nominal "3x" step
    # narrows the view 2.54x (3^0.85), not 3x.
    "hfov_1x": 55.1,
    "zoom_fov_exponent": 0.85,
    "vp_min_conf": 0.45,          # vanishing-point confidence needed to zoom past 3x
    # Aiming from the fixed lens: its vanishing point is stable while driving
    # (bolted to the car); keep confident ones from the last fixed_vp_window s
    # and aim when at least fixed_vp_min of them agree.
    "fixed_vp_conf": 0.4,
    "fixed_vp_window": 600,
    "fixed_vp_min": 4,
    "prior_vp_conf": 0.5,
    "moving_diff": 5.0,           # fixed-lens frame-to-frame change above this = driving
    "max_zoom_moving": 6,         # zoom cap while driving (12x/25x smear at night)         # confidence for a reading to join the per-camera road point
    # Frames with less detail than this (road_geometry.texture) and no cone
    # colour are not saved and stop the sweep zooming further: ceilings,
    # walls up close, noise-only frames at long zoom at night score 1–3;
    # a sharp cone on smooth asphalt with a blurred background can score 2,
    # hence the cone-colour exemption.
    "min_texture": 3.0,
    # Work-zone shots when a line of cones is in the 1x frame (colour-based,
    # road_geometry.cone_boxes): one at cone_centre_zoom on the middle of the
    # cones, then cone_zoom on each of the farthest far_cones — the start of
    # the taper. Works on curved roads/roundabouts with no vanishing point.
    # Modest zooms: PTZ pointing is good to ~0.5°, which is a fifth of the
    # frame at 12x; at 4x/8x the targeted cones stay in the shot.
    "cone_centre_zoom": 4,
    "cone_zoom": 8,
    "far_cones": 3,
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
    """Free space on the filesystem the dataset is written to."""
    path = DATASET_DIR
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return shutil.disk_usage(path).free / 1e9


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
        self.fixed_channel = None   # found by detect_layout()
        self.layout_checked = 0.0
        self.no_road_streak = 0
        self.last_thumb = None
        self.fixed_vps = []         # (time, x, y) confident vanishing points, fixed lens
        self.fixed_cones = (0.0, [])
        self.blank = 0
        self.match_scale = None
        self.clock_synced = 0.0
        self.last_vp_check = 0.0
        self.prev_thumb = None
        self.motion = (0.0, 0.0)       # (time, smoothed frame-to-frame change of the fixed lens)
        self.prior_file = os.path.join(os.path.dirname(SETTINGS_JSON), f"road_vp_{self.name}.json")
        self.prior_pts = self._load_prior()
        self.prior_dirty = 0

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

    def detect_layout(self) -> bool:
        """Work out from the camera itself which lens is which: the dashboard's
        ptz/role flags are inconsistent between cars. A TandemVu answers on
        channel 201 (fixed wide lens) and has its zoom lens on 101; a
        single-lens camera only has 101 and gets no sweeps."""
        if self.fixed_channel:
            return True
        if time.time() - self.layout_checked < 60:
            return False
        self.layout_checked = time.time()
        if self.snapshot(FIXED_CHANNEL):
            self.fixed_channel, self.has_ptz = FIXED_CHANNEL, True
        elif self.snapshot(PTZ_CHANNEL):
            self.fixed_channel, self.has_ptz = PTZ_CHANNEL, False
        else:
            log.debug("%s not answering — will retry", self.ip)
            return False
        log.info("%s: %s", self.ip, "dual-lens (fixed 201 + zoom 101)" if self.has_ptz else "single lens (101), no sweeps")
        return True

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
        # The cars lose power abruptly at the end of a shift: write to a temp
        # name, fsync, then rename, so a file is either whole or absent, and
        # fsync the index line so it never ends in a block of NUL bytes.
        path = os.path.join(folder, fname)
        with open(path + ".part", "wb") as f:
            f.write(jpeg)
            f.flush()
            os.fsync(f.fileno())
        os.replace(path + ".part", path)
        record = {"file": fname, "time": now.isoformat(timespec="milliseconds"), "device": self.device,
                  "camera_ip": self.ip, "lens": lens, "bytes": len(jpeg), **meta}
        with open(os.path.join(folder, "index.jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.counts[key] = self.counts.get(key, 0) + 1
        return True

    # ── camera clock ──
    def sync_clock(self):
        """Set the camera's clock from the car's (NTP over the modem) when
        they differ by more than 10 s. Cameras have no internet, drift by
        minutes, and one car's cameras were on 1970 — the time burnt into
        every frame is evidence, so it has to be right."""
        if dt.datetime.now().year < 2026:
            return                      # car clock not set yet
        url = f"http://{self.ip}/ISAPI/System/time"
        try:
            r = requests.get(url, auth=self.auth, timeout=TIMEOUT)
            m_t = re.search(r"<localTime>([^<]+)</localTime>", r.text)
            m_z = re.search(r"<timeZone>([^<]+)</timeZone>", r.text)
            if r.status_code != 200 or not m_t:
                return
            cam = dt.datetime.fromisoformat(m_t.group(1)[:19])
            drift = (cam - dt.datetime.now()).total_seconds()
            if abs(drift) <= 10:
                return
            now = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            tz = m_z.group(1) if m_z else "CST-4:00:00"
            xml = (f'<?xml version="1.0" encoding="UTF-8"?><Time><timeMode>manual</timeMode>'
                   f"<localTime>{now}</localTime><timeZone>{tz}</timeZone></Time>")
            r = requests.put(url, data=xml.encode(), headers={"Content-Type": "application/xml"},
                             auth=self.auth, timeout=TIMEOUT)
            log.info("%s camera clock was off by %+.0fs — set to %s (%s)", self.ip, drift, now,
                     "ok" if r.status_code == 200 else f"HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            log.debug("%s clock sync failed: %s", self.ip, e)

    # ── fixed lens ──
    def fixed_tick(self, cfg: dict):
        jpeg = self.snapshot(self.fixed_channel)
        if not jpeg:
            return
        now = time.time()
        libs = load_image_libs()
        thumb = RG.thumb_jpeg(jpeg) if libs else None
        if thumb is not None and self.prev_thumb is not None:
            # how fast the scene changes, frame to frame: parked 1–2, driving 7–28
            d = float(abs(thumb - self.prev_thumb).mean())
            self.motion = (now, d if self.motion[0] < now - 30 else 0.6 * self.motion[1] + 0.4 * d)
        if thumb is not None:
            self.prev_thumb = thumb
        if thumb is not None and self.last_thumb is not None:
            unchanged = float(abs(thumb - self.last_thumb).mean()) < cfg["fixed_diff"]
        else:
            # no OpenCV yet: same scene → near-identical JPEG size
            last = self.last_size.get("fixed")
            unchanged = bool(last) and abs(len(jpeg) - last) / last < 0.015
        keep = not unchanged or now - self.last_kept.get("fixed", 0) >= cfg["fixed_keep_every"]
        if not keep and not (libs and self.has_ptz and now - self.last_vp_check >= 20):
            return
        meta = {}
        if libs and self.has_ptz:
            self.last_vp_check = now
            # The fixed lens always looks down the road behind the TMA: its
            # vanishing point and cones are what the zoom lens is aimed at.
            img = decode(jpeg)
            if img is not None:
                r = RG.vanishing_point(img)
                cones = RG.cone_boxes(img)
                if r:
                    meta["vanishing_point"] = {"x": round(r[0], 4), "y": round(r[1], 4), "conf": round(r[2], 2)}
                    if r[2] >= cfg["fixed_vp_conf"]:
                        self.fixed_vps.append((now, r[0], r[1]))
                    if r[2] >= cfg["prior_vp_conf"]:
                        self._add_prior(r[0], r[1])
                if cones:
                    meta["cone_boxes"] = [[round(v, 4) for v in b] for b in cones]
                self.fixed_cones = (now, cones)
        if keep and self.save("fixed", jpeg, meta, cfg):
            self.last_size["fixed"] = len(jpeg)
            self.last_kept["fixed"] = now
            self.last_thumb = thumb

    def road_from_fixed(self, cfg: dict):
        """Median of the fixed lens's recent confident vanishing points, if
        they agree (the lens is bolted to the car, so on a straight road the
        point barely moves). None when parked off-road, on curves, in yards."""
        cutoff = time.time() - cfg["fixed_vp_window"]
        self.fixed_vps = [v for v in self.fixed_vps if v[0] >= cutoff][-60:]
        if len(self.fixed_vps) < cfg["fixed_vp_min"]:
            return None
        xs = sorted(v[1] for v in self.fixed_vps)
        ys = sorted(v[2] for v in self.fixed_vps)
        mx, my = xs[len(xs) // 2], ys[len(ys) // 2]
        near = sum(1 for _, x, y in self.fixed_vps if abs(x - mx) < 0.05 and abs(y - my) < 0.05)
        return (mx, my) if near >= 0.6 * len(self.fixed_vps) else None

    # The fixed lens is bolted to the car, so where the road behind it
    # vanishes barely changes from day to day. Remember it per camera: on
    # roads where a single frame can't be read (grass streaked by motion at
    # night, heavy traffic) the sweep still aims down the road.
    def _load_prior(self) -> list:
        try:
            with open(self.prior_file) as f:
                return [tuple(p) for p in json.load(f)["points"]][-400:]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _add_prior(self, x: float, y: float):
        self.prior_pts = (self.prior_pts + [(round(x, 4), round(y, 4))])[-400:]
        self.prior_dirty += 1
        if self.prior_dirty >= 10:
            self.prior_dirty = 0
            try:
                os.makedirs(os.path.dirname(self.prior_file), exist_ok=True)
                with open(self.prior_file + ".part", "w") as f:
                    json.dump({"points": self.prior_pts}, f)
                os.replace(self.prior_file + ".part", self.prior_file)
            except OSError:
                pass

    def road_prior(self):
        pts = self.prior_pts
        if len(pts) < 15:
            return None
        xs, ys = sorted(p[0] for p in pts), sorted(p[1] for p in pts)
        mx, my = xs[len(xs) // 2], ys[len(ys) // 2]
        near = sum(1 for x, y in pts if abs(x - mx) < 0.05 and abs(y - my) < 0.05)
        return (mx, my) if near >= 0.5 * len(pts) else None

    def bootstrap_prior(self, cfg: dict):
        """First run: learn the road point from fixed-lens images already on disk."""
        if len(self.prior_pts) >= 15 or not load_image_libs():
            return
        import glob
        files = sorted(glob.glob(os.path.join(DATASET_DIR, self.device, self.name, "fixed", "*", "*.jpg")))
        step = max(1, len(files) // 150)
        for path in files[::-1][::step][:150]:
            if not running:
                return
            img = cv2.imread(path)
            r = RG.vanishing_point(img) if img is not None else None
            if r and r[2] >= cfg["prior_vp_conf"]:
                self._add_prior(r[0], r[1])
        log.info("%s road point learnt from %d stored images: %s", self.ip, len(self.prior_pts), self.road_prior())

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
        th = math.tan(math.radians(cfg["hfov_1x"] / 2)) / zoom ** cfg["zoom_fov_exponent"]
        tv = th * aspect
        return (math.degrees(math.atan((x - 0.5) * 2 * th)),
                math.degrees(math.atan((y - 0.5) * 2 * tv)))

    class OperatorTookOver(Exception):
        pass

    def shot(self, az: int, el: int, zoom: float, cfg: dict, extra: dict | None = None) -> dict | None:
        """Move, settle, snapshot, save. Returns {"vp", "cones", "saved",
        "blank", "img"} or None. Frames with no detail (sky, a wall up close,
        noise-only night frames at long zoom) are not saved."""
        if last_operator_ptz() >= self.sweep_started:
            raise self.OperatorTookOver()
        z = int(round(zoom * 10))
        if not self.ptz_goto(az, el, z):
            return None
        time.sleep(cfg["settle_long_zoom"] if zoom >= 12 else cfg["settle"])
        jpeg = self.snapshot(PTZ_CHANNEL)
        if not jpeg:
            return None
        libs = load_image_libs()
        img = decode(jpeg) if libs else None
        meta = {"ptz": {"azimuth": az % 3600, "elevation": el, "zoom": z}}
        vp, cones, tex = None, [], None
        if img is not None:
            tex = RG.texture(img)
            meta["texture"] = round(tex, 1)
            if tex < cfg["min_texture"] and RG.cone_colour(img) < 0.002:
                self.blank += 1
                return {"vp": None, "cones": [], "saved": False, "blank": True, "img": img}
            if zoom <= 3:
                r = RG.vanishing_point(img)
                vp = r[:3] if r else None
            if zoom == 1:
                cones = RG.cone_boxes(img)
        if vp:
            meta["vanishing_point"] = {"x": round(vp[0], 4), "y": round(vp[1], 4), "conf": round(vp[2], 2)}
        if cones:
            meta["cone_boxes"] = [[round(v, 4) for v in b] for b in cones]
        meta.update(extra or {})
        return {"vp": vp, "cones": cones, "saved": self.save("ptz", jpeg, meta, cfg), "blank": False, "img": img}

    def fixed_to_ptz(self, ptz_img, cfg: dict):
        """Function (u, v) in the fixed lens's frame → (pan°, tilt°) offset
        from where the zoom lens points now (at 1x), from matching the two
        lenses' images; None when they don't match."""
        fjpeg = self.snapshot(self.fixed_channel)
        fimg = decode(fjpeg) if fjpeg else None
        if fimg is None or ptz_img is None:
            return None, None
        A, n = RG.match_lenses(fimg, ptz_img)
        if A is None:
            log.debug("%s lens match failed (%d matches)", self.ip, n)
            return None, fimg
        scale = math.hypot(A[0, 0], A[1, 0])        # zoom-frame px → fixed px
        if not 0.3 < scale < 0.9:                    # zoom 1x is ~0.5–0.65 of the fixed view
            log.debug("%s lens match implausible (scale %.2f)", self.ip, scale)
            return None, fimg
        Ai = cv2.invertAffineTransform(A)
        W = 960
        fh = fimg.shape[0] * W / fimg.shape[1]
        ph = ptz_img.shape[0] * W / ptz_img.shape[1]

        def to_angles(u, v, margin=0.05):
            """None outside the zoom lens's 1x view: the fixed lens is very
            wide and distorted, so the match only holds where they overlap."""
            x, y = Ai @ np.array([u * W, v * fh, 1.0])
            x, y = x / W, y / ph
            if not (-margin <= x <= 1 + margin and -margin <= y <= 1 + margin):
                return None
            return self.frame_to_angles(x, y, 1, cfg)
        self.match_scale = scale
        return to_angles, fimg

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
        n, aimed, n_cones, restore, how = 0, False, 0, True, ""
        self.blank = 0
        self.sweep_started = time.time() - 1
        try:
            az1, el1 = start["azimuth"], start["elevation"]
            # 1x from the current heading
            r = self.shot(az1, el1, 1, cfg, {"step": "overview"})
            n += bool(r and r["saved"])
            vp = r["vp"] if r and not r["blank"] else None
            cones = r["cones"] if r else []
            to_angles, fimg = self.fixed_to_ptz(r["img"], cfg) if r and r["img"] is not None else (None, None)

            # work zone: zoom on the cones themselves (also on curved roads).
            # The fixed lens sees more of the road than the zoom lens at 1x,
            # so its cones are used when the two lenses' images match.
            targets, pts = [], []
            if cones:
                pts = [self.frame_to_angles(b[0] + b[2] / 2, b[1] + b[3] / 2, 1, cfg) for b in cones]
            elif to_angles and fimg is not None:
                # cones the fixed lens sees inside the zoom lens's view (the
                # zoom lens's own detection missed them: small at night)
                fc = [(b, to_angles(b[0] + b[2] / 2, b[1] + b[3] / 2)) for b in RG.cone_boxes(fimg)]
                fc = [(b, a) for b, a in fc if a]
                if len(fc) >= 3:
                    cones, pts, how = [b for b, _ in fc], [a for _, a in fc], "fixed"
            if cones:
                n_cones = len(cones)
                cpan = sum(p[0] for p in pts) / len(pts)
                ctilt = sum(p[1] for p in pts) / len(pts)
                targets = [(cpan, ctilt, cfg["cone_centre_zoom"], "cones_centre")]
                targets += [(p[0], p[1], cfg["cone_zoom"], "far_cone") for p in pts[:cfg["far_cones"]]]
            for pan, tilt, zoom, step in targets:
                if not running:
                    return
                r = self.shot(az1 + round(pan * 10), el1 + round(tilt * 10), zoom, cfg,
                              {"step": step, "aim": how or "ptz"})
                n += bool(r and r["saved"])

            # far end of the road. Best: the fixed lens's stable vanishing
            # point, mapped onto the zoom lens. Fallback: the vanishing point
            # in the zoom lens's own 1x frame, refined at 3x.
            az0, el0 = az1, el1
            road = (self.road_from_fixed(cfg) or self.road_prior()) if to_angles else None
            road = to_angles(*road) if road else None
            if road:
                pan, tilt = road
                az0, el0 = az1 + round(pan * 10), el1 + round(tilt * 10)
                aimed, aim_by = True, "fixed"
            elif vp and vp[2] >= cfg["vp_min_conf"]:
                pan, tilt = self.frame_to_angles(vp[0], vp[1], 1, cfg)
                az0, el0 = az1 + round(pan * 10), el1 + round(tilt * 10)
                r = self.shot(az0, el0, 3, cfg, {"step": "aim"})
                n += bool(r and r["saved"])
                if r and r["vp"] and r["vp"][2] >= cfg["vp_min_conf"]:
                    pan, tilt = self.frame_to_angles(r["vp"][0], r["vp"][1], 3, cfg)
                    az0, el0 = az0 + round(pan * 10), el0 + round(tilt * 10)
                    aimed, aim_by = True, "ptz"
            # Long zoom from a moving car is a smear (night shutter); keep 12x/25x
            # for when the car stands still — a TMA parked at a work zone.
            moving = time.time() - self.motion[0] < 60 and self.motion[1] > cfg["moving_diff"]
            for zoom, pans, tilts in cfg["sweep_plan"]:
                if zoom > 3 and not aimed:
                    break
                if moving and zoom > cfg["max_zoom_moving"]:
                    break
                level_ok = False
                for tilt in tilts:
                    for pan in pans:
                        if not running:
                            return
                        r = self.shot(az0 + round(pan * 10), el0 + round(tilt * 10), zoom, cfg,
                                      {"step": "road", "aimed": aimed, **({"aim": aim_by} if aimed else {})})
                        n += bool(r and r["saved"])
                        level_ok |= bool(r and not r["blank"])
                if not level_ok:
                    break        # nothing but blank frames: more zoom won't help
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
        found_road = aimed or n_cones > 0
        self.no_road_streak = 0 if found_road else self.no_road_streak + 1
        parts = ([f"aimed at the road ({aim_by} lens)"] if aimed else []) + \
                ([f"{n_cones} cones zoomed ({how or 'ptz'} lens)"] if n_cones else []) + \
                ([f"{self.blank} blank skipped"] if self.blank else [])
        if aimed and moving:
            parts.append(f"moving — zoom ≤{cfg['max_zoom_moving']}x")
        log.info("%s PTZ sweep: %d images (%s)", self.ip, n, ", ".join(parts) if parts else
                 f"no road or cones found — 1x/3x only, {self.no_road_streak} in a row")

    def run(self):
        next_fixed = next_sweep = 0.0
        bootstrapped = False
        while running:
            cfg = settings()
            if not cfg["enabled"]:
                time.sleep(30)
                continue
            if not self.detect_layout():
                time.sleep(5)
                continue
            if not bootstrapped and self.has_ptz and load_image_libs():
                bootstrapped = True
                self.bootstrap_prior(cfg)
            now = time.time()
            if now - self.clock_synced > 3600:
                self.clock_synced = now
                self.sync_clock()
            if now >= next_fixed:
                next_fixed = now + cfg["fixed_interval"]
                self.fixed_tick(cfg)
            if now - last_operator_ptz() < cfg["operator_backoff"]:
                self.expected_pos = None   # operator moved it; start fresh later
            elif self.has_ptz and now >= next_sweep and now >= self.backoff_until:
                self.sweep(cfg)
                idle = self.no_road_streak >= cfg["idle_after"]
                next_sweep = time.time() + cfg["idle_sweep_interval" if idle else "sweep_interval"]
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
