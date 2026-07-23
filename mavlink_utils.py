import logging

from pymavlink import mavutil

logger = logging.getLogger("MAVLinkUtils")

MAVLINK_PATH_ETHERNET = "ethernet"
MAVLINK_PATH_SERIAL = "serial"

MSG_GLOBAL_POSITION_INT = 33
MSG_GPS_RAW_INT = 24

GCS_SYS_ID = 255
GCS_COMP_ID = 190
AUTOPILOT_COMP_ID = 1


def request_message_interval(
    conn,
    target_system: int,
    message_id: int,
    rate_hz: float,
    target_component: int = AUTOPILOT_COMP_ID,
) -> bool:
    """Ask PX4/ArduPilot to stream a MAVLink message at rate_hz (MAV_CMD_SET_MESSAGE_INTERVAL)."""
    if conn is None or target_system <= 0:
        return False
    try:
        interval_us = 0 if rate_hz <= 0 else int(1_000_000 / rate_hz)
        conn.mav.command_long_send(
            target_system,
            target_component,
            mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
            0,
            float(message_id),
            float(interval_us),
            0,
            0,
            0,
            0,
            0,
        )
        return True
    except Exception as exc:
        logger.warning(
            "SET_MESSAGE_INTERVAL failed (msg=%s sys=%s): %s",
            message_id,
            target_system,
            exc,
        )
        return False


def request_message_interval_udp(
    sock,
    target: tuple,
    target_system: int,
    message_id: int,
    rate_hz: float,
    target_component: int = AUTOPILOT_COMP_ID,
) -> bool:
    """Send SET_MESSAGE_INTERVAL on a raw UDP socket (PX4 ethernet partner path)."""
    if sock is None or target_system <= 0:
        return False
    try:
        interval_us = 0 if rate_hz <= 0 else int(1_000_000 / rate_hz)
        mav = mavutil.mavlink.MAVLink(None, srcSystem=GCS_SYS_ID, srcComponent=GCS_COMP_ID)
        msg = mav.command_long_encode(
            target_system,
            target_component,
            mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
            0,
            float(message_id),
            float(interval_us),
            0,
            0,
            0,
            0,
            0,
        )
        sock.sendto(msg.pack(mav), target)
        return True
    except Exception as exc:
        logger.warning(
            "UDP SET_MESSAGE_INTERVAL failed (msg=%s sys=%s): %s",
            message_id,
            target_system,
            exc,
        )
        return False


def pack_global_position_int(
    sys_id: int,
    seq: int,
    gps_msg,
    local_ned_msg=None,
    comp_id: int = AUTOPILOT_COMP_ID,
) -> bytes:
    """Build GLOBAL_POSITION_INT from GPS_RAW_INT (+ optional LOCAL_POSITION_NED for rel alt/vel)."""
    mav = mavutil.mavlink.MAVLink(None, srcSystem=sys_id, srcComponent=comp_id)
    time_boot_ms = int(getattr(gps_msg, "time_usec", 0) or 0) // 1000
    lat = int(getattr(gps_msg, "lat", 0) or 0)
    lon = int(getattr(gps_msg, "lon", 0) or 0)
    alt = int(getattr(gps_msg, "alt", 0) or 0)
    hdg = int(getattr(gps_msg, "cog", 0) or 0)

    relative_alt = 0
    vx = vy = vz = 0
    if local_ned_msg is not None:
        relative_alt = int(-float(getattr(local_ned_msg, "z", 0) or 0) * 1000.0)
        vx = int(float(getattr(local_ned_msg, "vx", 0) or 0) * 100.0)
        vy = int(float(getattr(local_ned_msg, "vy", 0) or 0) * 100.0)
        vz = int(float(getattr(local_ned_msg, "vz", 0) or 0) * 100.0)

    msg = mav.global_position_int_encode(
        time_boot_ms,
        lat,
        lon,
        alt,
        relative_alt,
        vx,
        vy,
        vz,
        hdg,
    )
    msg._header.seq = seq & 0xFF
    return msg.pack(mav)


def normalize_connection_type(conn_type: str) -> str:
    value = (conn_type or "").strip().lower()
    if value in ("", "ethernet", "udp", "udp_listen", "tcp_listen", "tcp_client"):
        return MAVLINK_PATH_ETHERNET
    if value in ("serial", "uart"):
        return MAVLINK_PATH_SERIAL
    if value in ("prefer_ethernet", "dual", "auto"):
        return "prefer_ethernet"
    return value


def is_pixhawk_heartbeat(msg) -> bool:
    if msg is None or msg.get_type() != "HEARTBEAT":
        return False

    mav = mavutil.mavlink
    mav_type = getattr(msg, "type", None)
    autopilot = getattr(msg, "autopilot", None)

    if mav_type in (mav.MAV_TYPE_GCS, mav.MAV_TYPE_ONBOARD_CONTROLLER):
        return False

    if autopilot in (
        mav.MAV_AUTOPILOT_PX4,
        mav.MAV_AUTOPILOT_ARDUPILOTMEGA,
        mav.MAV_AUTOPILOT_GENERIC,
    ):
        return True

    if autopilot == mav.MAV_AUTOPILOT_INVALID:
        return False

    if mav_type in (
        mav.MAV_TYPE_QUADROTOR,
        mav.MAV_TYPE_VTOL_TAILSITTER_QUADROTOR,
        mav.MAV_TYPE_FIXED_WING,
        mav.MAV_TYPE_HELICOPTER,
        mav.MAV_TYPE_VTOL_TILTROTOR,
    ):
        return True

    return False
