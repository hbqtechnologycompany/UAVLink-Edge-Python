"""PX4 Ethernet static IP setup (user-run, no systemd)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from mavlink_utils import normalize_connection_type

logger = logging.getLogger("EthernetSetup")


def _run_ip(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["ip", *args], capture_output=True, text=True)
    if result.returncode == 0:
        return result
    if os.geteuid() != 0:
        return subprocess.run(["sudo", "-n", "ip", *args], capture_output=True, text=True)
    return result


def resolve_interface(configured: str) -> str:
    name = (configured or "").strip()
    if name and Path(f"/sys/class/net/{name}").exists():
        return name
    for candidate in ("end0", "eth0"):
        if Path(f"/sys/class/net/{candidate}").exists():
            return candidate
    return name or "eth0"


def iface_has_ip(iface: str, ip: str) -> bool:
    result = subprocess.run(
        ["ip", "-4", "addr", "show", "dev", iface],
        capture_output=True,
        text=True,
    )
    return f"inet {ip}/" in result.stdout


def ensure_ethernet_ready(config) -> bool:
    """Assign ethernet.local_ip when auto_setup is enabled (before MAVLink bind)."""
    network = getattr(config, "network", {}) or {}
    conn_type = normalize_connection_type(network.get("connection_type", "serial"))
    if conn_type not in ("ethernet", "prefer_ethernet"):
        return True

    eth = getattr(config, "ethernet", {}) or {}
    local_ip = str(eth.get("local_ip") or "").strip()
    if not local_ip:
        return True

    configured = str(eth.get("interface") or "eth0")
    iface = resolve_interface(configured)
    if not Path(f"/sys/class/net/{iface}").exists():
        logger.warning("[NETWORK] Ethernet interface %s not found", iface)
        return False

    if configured and iface != configured:
        logger.info("[NETWORK] PX4 netdev: %s → %s", configured, iface)

    up = _run_ip("link", "set", "dev", iface, "up")
    if up.returncode != 0:
        logger.warning("[NETWORK] Could not bring %s up: %s", iface, (up.stderr or up.stdout).strip())

    if iface_has_ip(iface, local_ip):
        logger.info("[NETWORK] Ethernet %s already has %s", iface, local_ip)
        return True

    if not eth.get("auto_setup", False):
        logger.warning(
            "[NETWORK] %s has no %s and auto_setup=false — MAVLink bind may fail",
            iface,
            local_ip,
        )
        return False

    subnet = str(eth.get("subnet") or "24").strip()
    cidr = f"{local_ip}/{subnet}"
    add = _run_ip("addr", "add", cidr, "dev", iface)
    if add.returncode != 0:
        err = (add.stderr or add.stdout or "").strip()
        if "File exists" in err or iface_has_ip(iface, local_ip):
            logger.info("[NETWORK] Ethernet %s = %s (already present)", iface, cidr)
            return True
        logger.error("[NETWORK] Failed to set %s on %s: %s", cidr, iface, err)
        return False

    pixhawk_ip = str(eth.get("pixhawk_ip") or "")
    port = int(eth.get("pixhawk_port") or network.get("local_listen_port") or 14550)
    logger.info(
        "[NETWORK] Ethernet ready on %s = %s (PX4 %s:%d → Pi :%d)",
        iface,
        cidr,
        pixhawk_ip or "?",
        port,
        port,
    )
    return True


def start_ethernet_watchdog(config, stop_event, interval: float = 5.0) -> None:
    """Re-apply ethernet.local_ip if lost after boot/reset (NetworkManager may drop it)."""
    import threading
    import time

    network = getattr(config, "network", {}) or {}
    conn_type = normalize_connection_type(network.get("connection_type", "serial"))
    if conn_type not in ("ethernet", "prefer_ethernet"):
        return

    eth = getattr(config, "ethernet", {}) or {}
    local_ip = str(eth.get("local_ip") or "").strip()
    if not local_ip or not eth.get("auto_setup", False):
        return

    configured = str(eth.get("interface") or "eth0")
    iface = resolve_interface(configured)

    def _loop() -> None:
        had_ip = iface_has_ip(iface, local_ip)
        while not stop_event.is_set():
            try:
                has_ip = iface_has_ip(iface, local_ip)
                if not has_ip:
                    if had_ip:
                        logger.warning(
                            "[NETWORK] %s lost %s — re-applying static IP",
                            iface,
                            local_ip,
                        )
                    if ensure_ethernet_ready(config):
                        if not had_ip:
                            logger.info("[NETWORK] %s restored to %s", iface, local_ip)
                        had_ip = True
                    else:
                        had_ip = False
                else:
                    had_ip = True
            except Exception as exc:
                logger.warning("[NETWORK] Ethernet watchdog error: %s", exc)
            stop_event.wait(interval)

    threading.Thread(target=_loop, daemon=True, name="ethernet-watchdog").start()
    logger.info("[NETWORK] Ethernet watchdog started (%s → %s every %.0fs)", iface, local_ip, interval)
