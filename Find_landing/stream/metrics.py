"""Publisher stats — /tmp/camera_stream_stats_{id}.json + landing telemetry."""

import json
import os
import time


def stats_path(camera_id: int) -> str:
    return f"/tmp/camera_stream_stats_{camera_id}.json"


def landing_path(camera_id: int) -> str:
    return f"/tmp/camera_landing_{camera_id}.json"


def write_stats(config: dict, frames_sent: int, start_time: float,
                capture_fps: float, encode_drops: int, window_fps: float):
    try:
        payload = {
            "camera_id": config.get("camera_id", 0),
            "fps_sent": round(frames_sent / max(time.time() - start_time, 0.001), 1),
            "fps_window": round(window_fps, 1),
            "fps_capture": round(capture_fps, 1),
            "frames_sent": frames_sent,
            "encode_drops": encode_drops,
            "updated_at": time.time(),
        }
        path = stats_path(int(config.get("camera_id", 0)))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        pass


def write_landing_telemetry(camera_id: int, detection: dict, detections_count: int):
    """Expose Hướng 2 snapshot for REST / MAVLink bridge (IV-1, IV-2)."""
    try:
        det = detection or {"detected": False}
        payload = {
            "camera_id": camera_id,
            "detected": bool(det.get("detected")),
            "offset_x": det.get("offset_x"),
            "offset_y": det.get("offset_y"),
            "direction": det.get("direction"),
            "similarity": det.get("similarity"),
            "detections_count": detections_count,
            "updated_at": time.time(),
        }
        path = landing_path(camera_id)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        pass
