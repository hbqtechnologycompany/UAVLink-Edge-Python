"""Camera MAVLink bridge — HEARTBEAT + VIDEO_STREAM_* to cloud (Pi camera/camera_mavlink.go)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from pymavlink.dialects.v20 import common as mavlink_common

from web.camera_service import publish_path, read_stream_stats, _mediamtx

logger = logging.getLogger("CameraMavlink")

COMP_CAMERA = 100


def _mavlink_status_enabled(cfg) -> bool:
    camera = cfg.camera if hasattr(cfg, "camera") else {}
    value = camera.get("mavlink_status_enabled")
    if value is None:
        return True
    return bool(value)


def _status_hz(cfg) -> float:
    camera = cfg.camera if hasattr(cfg, "camera") else {}
    hz = float(camera.get("mavlink_status_hz", 1.0) or 1.0)
    return hz if hz > 0 else 1.0


def _aircraft_sys_id(forwarder) -> int:
    sys_id = int(getattr(forwarder, "_pixhawk_sys_id", 0) or 0)
    return sys_id if sys_id > 0 else 1


def _publish(forwarder, msg, sys_id: int, comp_id: int) -> None:
    if not forwarder.server_sock or not forwarder.auth_client.session_token:
        return
    mav = mavlink_common.MAVLink(None, srcSystem=sys_id, srcComponent=comp_id)
    try:
        buf = msg.pack(mav)
        forwarder.server_sock.sendto(buf, forwarder.target_addr)
    except OSError as exc:
        logger.debug("[CM5][MAVLINK] camera publish failed: %s", exc)


def _publish_cycle(cfg, forwarder, seq: int) -> None:
    sys_id = _aircraft_sys_id(forwarder)
    mav = mavlink_common.MAVLink(None, srcSystem=sys_id, srcComponent=COMP_CAMERA)

    hb = mav.heartbeat_encode(
        type=mavlink_common.MAV_TYPE_CAMERA,
        autopilot=mavlink_common.MAV_AUTOPILOT_INVALID,
        base_mode=0,
        custom_mode=0,
        system_status=mavlink_common.MAV_STATE_ACTIVE,
    )
    _publish(forwarder, hb, sys_id, COMP_CAMERA)

    mtx = _mediamtx(cfg)
    stream_id = 0
    for stream in cfg.camera.get("streams", []):
        if not stream.get("enabled", True):
            continue
        cam_id = int(stream.get("camera_id", 0))
        stats = read_stream_stats(cam_id, 5.0)
        if not stats:
            continue
        stream_id += 1
        w, h = 640, 480
        size = stream.get("size") or []
        if len(size) >= 2:
            w, h = int(size[0]), int(size[1])
        bitrate = int(stream.get("bitrate", 5000) or 5000)

        status = mav.video_stream_status_encode(
            stream_id,
            mavlink_common.VIDEO_STREAM_STATUS_FLAGS_RUNNING,
            float(stats.get("fps_window", 0) or 0),
            w,
            h,
            bitrate * 1000,
            0,
            0,
        )
        _publish(forwarder, status, sys_id, COMP_CAMERA)

        if seq == 1 or seq % 10 == 0:
            try:
                path = publish_path(cfg, cam_id)
                uri = f"rtsp://{mtx['host']}:{mtx['rtsp_port']}{path}"
                name = f"CM5_CAM{cam_id}"
                info = mav.video_stream_information_encode(
                    stream_id,
                    stream_id,
                    mavlink_common.VIDEO_STREAM_TYPE_RTSP,
                    mavlink_common.VIDEO_STREAM_STATUS_FLAGS_RUNNING,
                    float(stats.get("fps_window", 0) or 0),
                    w,
                    h,
                    bitrate * 1000,
                    0,
                    0,
                    name.encode("utf-8"),
                    uri.encode("utf-8"),
                    mavlink_common.VIDEO_STREAM_ENCODING_H264,
                )
                _publish(forwarder, info, sys_id, COMP_CAMERA)
            except Exception as exc:
                logger.debug("[CM5][MAVLINK] VIDEO_STREAM_INFORMATION cam%d: %s", cam_id, exc)


def start_camera_mavlink_bridge(cfg, forwarder, stop_event: Optional[threading.Event] = None) -> None:
    if not cfg.camera.get("enabled", False):
        return
    if not _mavlink_status_enabled(cfg):
        return

    hz = _status_hz(cfg)
    interval = 1.0 / hz
    logger.info("[CM5][MAVLINK] Camera bridge comp=%d @ %.1f Hz", COMP_CAMERA, hz)

    def _loop() -> None:
        seq = 0
        while stop_event is None or not stop_event.is_set():
            seq = (seq + 1) & 0xFF
            if seq == 0:
                seq = 1
            _publish_cycle(cfg, forwarder, seq)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="camera-mavlink").start()
