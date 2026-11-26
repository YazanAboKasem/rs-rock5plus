#!/usr/bin/env python3
"""
RoadShield — road_geometry.py

Road vanishing point from a single frame: lane markings / road edges are
straight lines converging on the point far down the road, which is where a
taper, approaching traffic and distant cones are — the place to aim the zoom.

    python road_geometry.py img.jpg [out.jpg]  → prints "x y conf" (0..1 coords)

Method: CLAHE + Canny on the lower 70% of the frame, probabilistic Hough,
keep lines between 12° and 80° from horizontal, then pick the point that the
most lines pass close to (RANSAC over pairwise intersections, weighted by
line length).
"""
import math
import sys

import cv2
import numpy as np


def thumb_jpeg(jpeg: bytes):
    """64x36 grayscale float thumbnail (decoded at 1/8 scale — cheap), or None."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
    return None if img is None else cv2.resize(img, (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32)


def cones_jpeg(jpeg: bytes):
    """Cone boxes (x, y, w, h in 0..1), far→near; empty if fewer than 3."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    return [] if img is None else cone_boxes(img)


def vanishing_point_jpeg(jpeg: bytes):
    """(x, y, confidence) in 0..1 image coordinates, or None."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    r = vanishing_point(img)
    return None if r is None else r[:3]


def vanishing_point(img):
    h, w = img.shape[:2]
    scale = 1280 / w
    small = cv2.resize(img, (1280, int(h * scale)))
    H, W = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges[: int(H * 0.30)] = 0  # sky / skyline / OSD timestamp
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60, minLineLength=W // 12, maxLineGap=12)
    if lines is None:
        return None
    segs = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        ang = abs(math.degrees(math.atan2(y2 - y1, x2 - x1)))
        ang = min(ang, 180 - ang)
        if 12 <= ang <= 80:
            segs.append((x1, y1, x2, y2, math.hypot(x2 - x1, y2 - y1)))
    if len(segs) < 4:
        return None
    segs = np.array(segs, dtype=np.float64)
    # homogeneous lines
    p1 = np.c_[segs[:, 0], segs[:, 1], np.ones(len(segs))]
    p2 = np.c_[segs[:, 2], segs[:, 3], np.ones(len(segs))]
    L = np.cross(p1, p2)
    L /= np.linalg.norm(L[:, :2], axis=1, keepdims=True)
    wts = segs[:, 4] ** 2   # long lines (lane markings, kerbs) outvote grass/gravel texture
    rng = np.random.default_rng(0)
    best, best_score = None, 0
    tol = W * 0.012
    for _ in range(600):
        i, j = rng.choice(len(segs), 2, replace=False)
        p = np.cross(L[i], L[j])
        if abs(p[2]) < 1e-9:
            continue
        x, y = p[0] / p[2], p[1] / p[2]
        if not (-0.2 * W < x < 1.2 * W and -0.2 * H < y < 0.9 * H):
            continue
        d = np.abs(L @ np.array([x, y, 1.0]))
        score = wts[d < tol].sum()
        if score > best_score:
            best, best_score = (x, y), score
    if best is None:
        return None
    # refine: least squares over inliers
    d = np.abs(L @ np.array([best[0], best[1], 1.0]))
    inl = d < tol
    A, b = L[inl, :2], -L[inl, 2]
    sol, *_ = np.linalg.lstsq(A * wts[inl, None], b * wts[inl], rcond=None)
    conf = float(wts[inl].sum() / wts.sum())
    # A road's edges and lane lines converge from both sides: lines on the left
    # of the vanishing point lean one way, on the right the other. Ceiling
    # tiles, fences, tarps and building facades give one family of parallel
    # lines instead. Score the weaker side; < ~0.2 is not a road.
    slope = (segs[inl, 3] - segs[inl, 1]) / np.where(segs[inl, 2] == segs[inl, 0], 1e-6, segs[inl, 2] - segs[inl, 0])
    w_in = wts[inl]
    neg, pos = w_in[slope < 0].sum(), w_in[slope > 0].sum()
    two_sided = float(min(neg, pos) / max(neg + pos, 1e-6))
    conf *= min(1.0, two_sided / 0.2)
    return float(sol[0] / W), float(sol[1] / H), conf, segs, inl, scale


def texture(img) -> float:
    """Detail in a frame: std of the Laplacian on a 640px-wide grey copy.
    Blank sky, a wall or ceiling up close, an out-of-focus or noise-only night
    frame at long zoom score < ~6; a street scene scores 15+."""
    g = cv2.cvtColor(cv2.resize(img, (640, int(img.shape[0] * 640 / img.shape[1]))), cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (3, 3), 0)
    h = g.shape[0]
    return float(cv2.Laplacian(g[int(h * 0.09): int(h * 0.9)], cv2.CV_32F).std())


def cone_colour(img) -> float:
    """Fraction of the frame in traffic-cone orange/red (0..1)."""
    small = cv2.resize(img, (320, int(img.shape[0] * 320 / img.shape[1])))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (0, 110, 90), (14, 255, 255)) | cv2.inRange(hsv, (165, 110, 90), (180, 255, 255))
    return float(m.mean() / 255)


def match_lenses(fixed, ptz, min_inliers=15):
    """Where the zoom lens's frame sits inside the fixed lens's frame.
    Returns (A, inliers): A is a 2x3 similarity transform mapping zoom-frame
    pixels (both images resized to 960 wide) to fixed-frame pixels, or None.
    The camera's date/time and channel-name OSD is identical in both lenses
    and would match itself — masked out."""
    W = 960
    def prep(x):
        g = cv2.cvtColor(cv2.resize(x, (W, int(x.shape[0] * W / x.shape[1]))), cv2.COLOR_BGR2GRAY)
        g = cv2.createCLAHE(2.0, (8, 8)).apply(g)
        m = np.full(g.shape, 255, np.uint8)
        m[: int(g.shape[0] * 0.09)] = 0
        m[int(g.shape[0] * 0.9):] = 0
        return g, m
    (gf, mf), (gp, mp) = prep(fixed), prep(ptz)
    sift = cv2.SIFT_create(3000)
    kf, df = sift.detectAndCompute(gf, mf)
    kp, dp = sift.detectAndCompute(gp, mp)
    if df is None or dp is None or len(kf) < 2 or len(kp) < 2:
        return None, 0
    pairs = cv2.BFMatcher().knnMatch(dp, df, k=2)
    good = [a for a, b in (x for x in pairs if len(x) == 2) if a.distance < 0.75 * b.distance]
    if len(good) < min_inliers:
        return None, len(good)
    src = np.float32([kp[g.queryIdx].pt for g in good])
    dst = np.float32([kf[g.trainIdx].pt for g in good])
    A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    n = int(inl.sum()) if inl is not None else 0
    return (A, n) if A is not None and n >= min_inliers else (None, n)


def cone_boxes(img, horizon=0.35):
    """Traffic cones by colour: upright saturated orange/red blobs below the
    horizon with a bright, unsaturated (reflective white) band just above.
    Returns [(x, y, w, h) in 0..1 image coords] sorted far→near (small→big);
    empty unless at least 3 are found (cones come in lines)."""
    h, w = img.shape[:2]
    small = cv2.resize(img, (1280, int(h * 1280 / w)))
    H, W = small.shape[:2]
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (0, 110, 90), (14, 255, 255)) | cv2.inRange(hsv, (165, 110, 90), (180, 255, 255))
    m[: int(H * horizon)] = 0          # cones stand on the road, below the horizon
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.dilate(m, np.ones((5, 3), np.uint8))
    white = cv2.inRange(hsv, (0, 0, 150), (180, 70, 255))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m)
    out = []
    for x, y, bw, bh, area in stats[1:]:
        if area < 25 or bh < 8 or bh > H * 0.5 or bh / max(bw, 1) < 1.0 or area / (bw * bh) < 0.25:
            continue
        # reflective band: white pixels in the strip just above / inside the top of the blob
        y0, y1 = max(0, y - bh // 2), y + bh // 3
        band = white[y0:y1, max(0, x - 1):x + bw + 1]
        if band.size == 0 or band.mean() / 255 < 0.08:
            continue
        out.append((x / W, y / H, bw / W, bh / H))
    return sorted(out, key=lambda b: b[3]) if len(out) >= 3 else []


if __name__ == "__main__":
    img = cv2.imread(sys.argv[1])
    r = vanishing_point(img)
    if r is None:
        print("none")
        sys.exit(1)
    x, y, conf, segs, inl, scale = r
    print(f"{x:.3f} {y:.3f} {conf:.2f}")
    if len(sys.argv) > 2:
        out = cv2.resize(img, (1280, int(img.shape[0] * scale)))
        for (x1, y1, x2, y2, _), ok in zip(segs, inl):
            cv2.line(out, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0) if ok else (0, 0, 255), 2)
        cv2.circle(out, (int(x * out.shape[1]), int(y * out.shape[0])), 14, (0, 255, 255), 3)
        cv2.imwrite(sys.argv[2], out)
