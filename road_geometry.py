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
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60, minLineLength=W // 25, maxLineGap=12)
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
    wts = segs[:, 4]
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
    return float(sol[0] / W), float(sol[1] / H), conf, segs, inl, scale


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
