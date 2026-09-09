#!/usr/bin/env python3
"""One-off test: person detect -> crop -> upscale -> PPE detect, on live cam5."""
import os
import time

import cv2
from ultralytics import YOLO

OUT_DIR = "ai/ppe_crop_test"
os.makedirs(OUT_DIR, exist_ok=True)

person_model = YOLO("yolov8n.pt")
ppe_model = YOLO("ai/models/ppe_yolov8n.pt")

cap = cv2.VideoCapture("rtsp://127.0.0.1:8554/cam5", cv2.CAP_FFMPEG)  # MAIN stream this time
print("[ppe-test] watching cam5 MAIN stream for a person...")

seen = 0
start = time.time()
while time.time() - start < 300 and seen < 8:
    ok, frame = cap.read()
    if not ok:
        time.sleep(0.2)
        continue

    results = person_model.predict(frame, classes=[0], conf=0.4, verbose=False)
    boxes = results[0].boxes
    if boxes is None or len(boxes) == 0:
        time.sleep(0.2)
        continue

    best = boxes[boxes.conf.argmax()]
    x1, y1, x2, y2 = [int(v) for v in best.xyxy[0].tolist()]
    h, w = frame.shape[:2]
    pad = 15
    x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
    x2, y2 = min(w, x2 + pad), min(h, y2 + pad)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        continue

    seen += 1
    # Main stream crops are already much bigger than sub-stream ones, so a
    # smaller upscale factor is enough (and keeps inference fast).
    crop_big = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    cv2.imwrite(f"{OUT_DIR}/raw_{seen}.jpg", crop_big)

    ppe_results = ppe_model.predict(crop_big, conf=0.15, verbose=False)
    dets = [f"{ppe_model.names[int(b.cls)]}:{float(b.conf):.2f}" for b in ppe_results[0].boxes]
    print(f"[ppe-test] person crop {seen}: size={crop.shape[1]}x{crop.shape[0]} person_conf={float(best.conf):.2f} -> "
          + (", ".join(dets) if dets else "NOTHING"))
    ppe_results[0].save(filename=f"{OUT_DIR}/ppe_{seen}.jpg")

    time.sleep(1.0)  # avoid re-grabbing the same person instantly

cap.release()
print(f"[ppe-test] done, {seen} person crop(s) tested.")
