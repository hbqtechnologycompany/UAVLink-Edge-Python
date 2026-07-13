"""Custom MAVLink messages — parity with Pi_CM5 mavlink_custom package."""

from __future__ import annotations

import hashlib
import os
import struct
import time
from typing import Optional

from pymavlink.generator.mavcrc import x25crc

# Message IDs
MSG_SESSION_HEARTBEAT = 42999
MSG_DRONEBRIDGE_STATUS = 42998

# GPS diagnosis codes (DRONEBRIDGE_STATUS)
GPS_DIAG_UNKNOWN = 0
GPS_DIAG_NO_PX4_STREAM = 1
GPS_DIAG_PX4_NO_FIX = 2
GPS_DIAG_PX4_OK = 3
GPS_DIAG_PX4_LOCAL_ONLY = 4

# Companion identity (CM5 onboard — not PX4)
COMPANION_SYS_ID = 245
COMP_ONBOARD = 191

_CRC_EXTRA = {
    MSG_SESSION_HEARTBEAT: 0,
    MSG_DRONEBRIDGE_STATUS: 0,
}


def _mavlink_v2_frame(
    msg_id: int,
    payload: bytes,
    sys_id: int,
    comp_id: int,
    seq: int,
) -> bytes:
    header = struct.pack(
        "<BBBBBBB",
        0xFD,
        len(payload),
        0,
        0,
        seq & 0xFF,
        sys_id & 0xFF,
        comp_id & 0xFF,
    ) + struct.pack("<I", msg_id)[:3]
    crc = x25crc(header[1:] + payload)
    crc.accumulate(bytes([_CRC_EXTRA.get(msg_id, 0)]))
    return header + payload + struct.pack("<H", crc.crc)


def build_session_heartbeat_payload(
    token: str,
    expires_at: int,
    sequence: int,
    pixhawk_active: int,
) -> bytes:
    """Layout: expires_at(u32) | sequence(u16) | pixhawk_active(u8) | SHA256(token)(32)."""
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    payload = struct.pack("<IHB", expires_at, sequence, pixhawk_active & 0xFF)
    return payload + token_hash


def build_session_heartbeat_frame(
    sys_id: int,
    comp_id: int,
    seq: int,
    token: str,
    expires_at: int,
    sequence: int,
    pixhawk_active: int,
) -> bytes:
    payload = build_session_heartbeat_payload(token, expires_at, sequence, pixhawk_active)
    return _mavlink_v2_frame(MSG_SESSION_HEARTBEAT, payload, sys_id, comp_id, seq)


def build_session_heartbeat_frame_shifted(
    sys_id: int,
    comp_id: int,
    seq: int,
    token_hex: str,
    expires_at: int,
    sequence: int,
    pixhawk_active: int,
) -> bytes:
    """Shifted mode for server builds that extract token from byte offset 7."""
    decoded = bytes.fromhex(token_hex)
    if len(decoded) != 32:
        raise ValueError("session token must be 64-char hex (32 bytes)")
    shifted = b"\x00" + decoded[:31]
    payload = struct.pack("<IHB", expires_at, sequence, pixhawk_active & 0xFF)
    payload += shifted
    return _mavlink_v2_frame(MSG_SESSION_HEARTBEAT, payload, sys_id, comp_id, seq)


def build_dronebridge_status_frame(
    sys_id: int,
    comp_id: int,
    seq: int,
    *,
    timestamp_ms: int,
    gps_fix_type: int,
    gps_satellites: int,
    gps_px4_streaming: int,
    gps_diagnosis: int,
    camera0_live: int,
    camera1_live: int,
) -> bytes:
    payload = struct.pack(
        "<IBBBBBBB",
        timestamp_ms,
        gps_fix_type & 0xFF,
        gps_satellites & 0xFF,
        gps_px4_streaming & 0xFF,
        gps_diagnosis & 0xFF,
        camera0_live & 0xFF,
        camera1_live & 0xFF,
        0,
    )
    return _mavlink_v2_frame(MSG_DRONEBRIDGE_STATUS, payload, sys_id, comp_id, seq)


def session_hb_mode() -> str:
    return os.environ.get("DRONEBRIDGE_SESSION_HB_MODE", "shifted").strip().lower()


def forward_gps_raw_int(network_cfg: dict) -> bool:
    value = network_cfg.get("forward_gps_raw_int")
    if value is None:
        return True
    return bool(value)
