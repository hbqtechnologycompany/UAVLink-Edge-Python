"""Cloud egress readiness — reads netmon JSON when present (Python-only, no PBR setup)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

from paths import module_4g_path, resolve_network_status_file

logger = logging.getLogger("CloudEgress")

NETWORK_STATUS_FILE = Path("/run/dronebridge/network_status.json")


def _read_status() -> Optional[dict]:
    path = resolve_network_status_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cloud_ready() -> bool:
    raw = _read_status()
    if not raw:
        return False
    return bool(raw.get("cloud_ready"))


def physical_egress() -> Tuple[str, str, bool]:
    raw = _read_status()
    if not raw:
        return "", "", False

    planes = raw.get("planes") or {}
    cloud_uplink = planes.get("cloud_uplink") or {}
    if cloud_uplink.get("ready"):
        iface = str(cloud_uplink.get("interface") or "")
        ip = str(cloud_uplink.get("ip") or "")
        if iface and ip:
            return iface, ip, True

    iface = str(raw.get("active_interface") or "")
    block_key = {"wwan0": "4g", "wlan0": "wifi"}.get(iface, "")
    if block_key:
        block = raw.get(block_key) or {}
        ip = str(block.get("ip") or block.get("ip_address") or "")
        online = bool(block.get("online"))
        if ip and ip != "null":
            return iface, ip, online
    return iface, "", False


def _has_wwan() -> bool:
    return Path("/sys/class/net/wwan0").exists()


def _wifi_online(raw: Optional[dict]) -> Tuple[str, str]:
    if not raw:
        return "", ""
    wifi = raw.get("wifi") or {}
    if wifi.get("online"):
        ip = str(wifi.get("ip") or wifi.get("ip_address") or "")
        return "wlan0", ip
    planes = raw.get("planes") or {}
    wlan = planes.get("wlan_admin") or {}
    ip = str(wlan.get("ip") or "")
    if ip:
        return "wlan0", ip
    return "", ""


def wait_for_cloud_egress(max_wait_sec: float = 120.0) -> Tuple[str, str, bool]:
    """Block until cloud_ready or timeout. User-run: không chờ 4G khi không có wwan0."""
    if not NETWORK_STATUS_FILE.exists():
        if not module_4g_path("connection_manager.py").exists():
            logger.info("[STARTUP] No netmon — skipping cloud_ready wait (user-run mode)")
            return "", "", False
        logger.info("[STARTUP] Netmon starting — brief cloud_ready wait (10s)")
        max_wait_sec = min(max_wait_sec, 10.0)
    elif not _has_wwan():
        raw = _read_status()
        iface, ip = _wifi_online(raw)
        if iface:
            logger.info(
                "[STARTUP] No 4G modem — WiFi available (%s), skip long cloud_ready wait",
                ip or iface,
            )
            return iface, ip, False
        logger.info("[STARTUP] No wwan0 — short cloud_ready wait (5s)")
        max_wait_sec = min(max_wait_sec, 5.0)

    if max_wait_sec <= 0:
        return "", "", False

    logger.info("[STARTUP] Waiting for cloud_ready from netmon (max %.0fs)...", max_wait_sec)
    deadline = time.time() + max_wait_sec
    while time.time() < deadline:
        if cloud_ready():
            iface, ip, _ = physical_egress()
            return iface, ip, True
        time.sleep(2)
    logger.warning("[STARTUP] cloud_ready timeout — continuing (auth/MAVLink will retry)")
    return "", "", False
