"""LANDING_TARGET uplink from vision telemetry — Pi camera/landing_mavlink.go."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from pymavlink.dialects.v20 import common as mavlink_common

from web.camera_service import read_landing_telemetry

logger = logging.getLogger("LandingMavlink")


def _publish_generated(forwarder, msg) -> None:
    if not forwarder.server_sock or not forwarder.auth_client.session_token:
        return
    sys_id = int(getattr(forwarder, "_pixhawk_sys_id", 0) or 0) or 1
    comp_id = 1
    mav = mavlink_common.MAVLink(None, srcSystem=sys_id, srcComponent=comp_id)
    try:
        buf = msg.pack(mav)
        forwarder.server_sock.sendto(buf, forwarder.target_addr)
    except OSError as exc:
        logger.debug("[LANDING][MAVLINK] write failed: %s", exc)


def _landing_target_from_telemetry(lt: dict):
    mav = mavlink_common.MAVLink(None)
    angle_x = float(lt.get("offset_x") or 0.0)
    angle_y = float(lt.get("offset_y") or 0.0)
    similarity = lt.get("similarity")
    size = 0.25
    if similarity is not None and float(similarity) > 0:
        size = min(1.0, float(similarity))
    return mav.landing_target_encode(
        int(time.time() * 1_000_000),
        0,
        mavlink_common.MAV_FRAME_BODY_FRD,
        angle_x,
        angle_y,
        1.0,
        size,
        size,
        0.0,
        0.0,
        0.0,
        type=mavlink_common.LANDING_TARGET_TYPE_VISION_FIDUCIAL,
        position_valid=0,
    )


def start_landing_mavlink_bridge(cfg, forwarder, stop_event: Optional[threading.Event] = None) -> None:
    landing = cfg.landing if hasattr(cfg, "landing") else {}
    if not landing.get("mavlink_enabled", False):
        return

    camera_id = int(landing.get("mavlink_camera_id", landing.get("mavlink_camera", 0)) or 0)
    hz = float(landing.get("mavlink_hz", 10) or 10)
    if hz <= 0:
        hz = 10
    interval = 1.0 / hz
    logger.info("[LANDING][MAVLINK] LANDING_TARGET bridge cam%d @ %.1f Hz", camera_id, hz)

    def _loop() -> None:
        while stop_event is None or not stop_event.is_set():
            lt = read_landing_telemetry(camera_id, 2.0)
            if lt and lt.get("detected"):
                msg = _landing_target_from_telemetry(lt)
                _publish_generated(forwarder, msg)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="landing-mavlink").start()
