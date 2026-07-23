"""MAVLink telemetry cache for /api/telemetry and /api/status enrichment."""

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

TELEMETRY_STALE_SEC = 5.0

_PX4_MAIN_MODES = {
    1: "MANUAL",
    2: "ALTCTL",
    3: "POSCTL",
    4: "AUTO",
    5: "ACRO",
    6: "OFFBOARD",
    7: "STABILIZED",
    8: "RATTITUDE",
    9: "SIMPLE",
}


def _px4_flight_mode(custom_mode: int) -> str:
    main = (int(custom_mode) >> 16) & 0xFF
    return _PX4_MAIN_MODES.get(main, f"MODE_{main}")


def _gps_fix_label(fix_type: int, sats: int) -> str:
    if fix_type >= 3 and sats >= 6:
        return "3D Fix"
    if fix_type >= 3:
        return "3D"
    if fix_type == 2:
        return "2D"
    if fix_type == 1:
        return "No Fix"
    return "Unknown"


class TelemetryCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.connected = False
        self.last_update: Optional[datetime] = None
        self.flight_mode = ""
        self.altitude_m = 0.0
        self.speed_ms = 0.0
        self.climb_ms = 0.0
        self.gps_fix = 0
        self.satellites = 0
        self.gps_lock = ""
        self.latitude_deg: Optional[float] = None
        self.longitude_deg: Optional[float] = None
        self.hdop: Optional[float] = None
        self.heading_deg: Optional[float] = None
        self.voltage_v = 0.0
        self.battery_pct = -1.0

    def _touch(self) -> None:
        self.connected = True
        self.last_update = datetime.now(timezone.utc)

    def feed(self, msg) -> None:
        msg_type = msg.get_type()
        with self._lock:
            if msg_type == "HEARTBEAT":
                self.flight_mode = _px4_flight_mode(getattr(msg, "custom_mode", 0))
                self._touch()
            elif msg_type == "VFR_HUD":
                self.altitude_m = float(getattr(msg, "alt", 0) or 0)
                self.speed_ms = float(getattr(msg, "groundspeed", 0) or 0)
                self.climb_ms = float(getattr(msg, "climb", 0) or 0)
                self._touch()
            elif msg_type == "GLOBAL_POSITION_INT":
                lat = getattr(msg, "lat", None)
                lon = getattr(msg, "lon", None)
                if lat is not None:
                    self.latitude_deg = float(lat) / 1e7
                if lon is not None:
                    self.longitude_deg = float(lon) / 1e7
                hdg = getattr(msg, "hdg", None)
                if hdg is not None and int(hdg) != 65535:
                    self.heading_deg = float(hdg) / 100.0
                rel_alt = getattr(msg, "relative_alt", None)
                if rel_alt is not None:
                    self.altitude_m = float(rel_alt) / 1000.0
                vz = getattr(msg, "vz", None)
                if vz is not None:
                    self.climb_ms = float(-vz) / 100.0
                self._touch()
            elif msg_type == "GPS_RAW_INT":
                self.gps_fix = int(getattr(msg, "fix_type", 0) or 0)
                self.satellites = int(getattr(msg, "satellites_visible", 0) or 0)
                self.gps_lock = _gps_fix_label(self.gps_fix, self.satellites)
                lat = getattr(msg, "lat", None)
                lon = getattr(msg, "lon", None)
                if lat is not None:
                    self.latitude_deg = float(lat) / 1e7
                if lon is not None:
                    self.longitude_deg = float(lon) / 1e7
                eph = getattr(msg, "eph", None)
                if eph is not None and int(eph) != 65535:
                    self.hdop = float(eph) / 100.0
                cog = getattr(msg, "cog", None)
                if cog is not None and int(cog) != 65535:
                    self.heading_deg = float(cog) / 100.0
                alt_mm = getattr(msg, "alt", None)
                if alt_mm is not None and int(alt_mm) != -1:
                    self.altitude_m = float(alt_mm) / 1000.0
                self._touch()
            elif msg_type == "SYS_STATUS":
                vb = getattr(msg, "voltage_battery", None)
                if vb:
                    self.voltage_v = float(vb) / 1000.0
                br = getattr(msg, "battery_remaining", None)
                if br is not None and int(br) >= 0:
                    self.battery_pct = float(br)
                self._touch()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            valid = (
                self.connected
                and self.last_update is not None
                and (datetime.now(timezone.utc) - self.last_update).total_seconds() <= TELEMETRY_STALE_SEC
            )
            return {
                "valid": valid,
                "connected": self.connected and valid,
                "flight_mode": self.flight_mode if valid else "",
                "altitude_m": self.altitude_m if valid else 0.0,
                "speed_ms": self.speed_ms if valid else 0.0,
                "climb_ms": self.climb_ms if valid else 0.0,
                "gps_fix": self.gps_fix if valid else 0,
                "satellites": self.satellites if valid else 0,
                "gps_lock": self.gps_lock if valid else "",
                "latitude_deg": self.latitude_deg if valid else None,
                "longitude_deg": self.longitude_deg if valid else None,
                "hdop": self.hdop if valid else None,
                "heading_deg": self.heading_deg if valid else None,
                "voltage_v": self.voltage_v if valid else 0.0,
                "battery_pct": self.battery_pct if valid else -1.0,
                "last_update": self.last_update.isoformat() if self.last_update else None,
            }


global_telemetry = TelemetryCache()
