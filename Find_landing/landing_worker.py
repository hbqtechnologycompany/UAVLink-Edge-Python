#!/usr/bin/env python3
"""III-3: CV worker tách khỏi streamer — chạy lores-only, ghi /tmp/camera_landing_{id}.json.

Usage:
  python3 landing_worker.py camera_config_1.json

Streamer đọc telemetry khi config external_landing=true hoặc DRONEBRIDGE_EXTERNAL_LANDING=1.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from threading import Event

from camera_manager import get_camera_manager
from processing.pipeline import build_pipeline
from stream.metrics import write_landing_telemetry
from stream.wire_format import sensor_frame_to_bgr


def _load_config(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def _lores_size(config: dict) -> tuple[int, int]:
    custom = config.get("lores_size")
    if isinstance(custom, (list, tuple)) and len(custom) >= 2:
        return int(custom[0]), int(custom[1])
    return 320, 240


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: landing_worker.py <camera_config_N.json>", file=sys.stderr)
        return 2

    config = _load_config(sys.argv[1])
    camera_id = int(config.get("camera_id", 0))
    find_landing_dir = os.path.dirname(os.path.abspath(__file__))
    running = Event()
    running.set()

    def _stop(*_args):
        running.clear()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    cam_manager = get_camera_manager()
    user_id = "landing_worker"
    cam_cfg = {
        "format": config.get("format", "RGB888"),
        "size": tuple(config["size"]),
        "processing_lores": True,
        "lores_size": _lores_size(config),
    }
    if config.get("libcamera_index") is not None:
        cam_cfg["libcamera_index"] = int(config["libcamera_index"])

    camera = cam_manager.get_camera(camera_id, user_id, cam_cfg)
    if camera is None or isinstance(camera, dict):
        print(f"✗ landing_worker: cannot open camera {camera_id}", file=sys.stderr)
        return 1
    if not cam_manager.has_lores(camera_id):
        print(f"✗ landing_worker: camera {camera_id} has no lores stream", file=sys.stderr)
        cam_manager.release_camera(camera_id, user_id)
        return 1

    byte_order = cam_manager.get_lores_byte_order(camera_id)
    processing = build_pipeline(config, find_landing_dir, running, overlay_processor=False)
    if not processing:
        print("✗ landing_worker: detection/overlay disabled in config", file=sys.stderr)
        cam_manager.release_camera(camera_id, user_id)
        return 1
    processing.start()
    print(f" landing_worker cam{camera_id}: lores {_lores_size(config)} → {find_landing_dir}")

    fps = max(int(config.get("framerate", 30)), 1)
    interval = 1.0 / fps
    fid = 0
    last_tick = 0.0

    try:
        while running.is_set():
            now = time.time()
            if now - last_tick < interval:
                time.sleep(0.002)
                continue
            last_tick = now
            if processing.wants_feed(fid):
                frame = cam_manager.capture_lores(camera_id, user_id)
                if frame is not None:
                    bgr = sensor_frame_to_bgr(frame, byte_order)
                    processing.submit(fid, bgr)
            det = processing.latest_detection()
            write_landing_telemetry(camera_id, det, processing.detections_count)
            fid += 1
    finally:
        processing.stop()
        cam_manager.release_camera(camera_id, user_id)
        print(" landing_worker stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
