"""Network mode API helpers — parity with Pi_CM5 web/network_mode.go."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from paths import module_4g_path, project_path
import network_controller
from web.network_helpers import read_network_status

logger = logging.getLogger("NetworkMode")

VALID_MODES = frozenset({"prefer_4g", "4g_only", "wifi_only"})


def mode_to_legacy_priority(mode: str) -> str:
    if mode == "wifi_only":
        return "wifi"
    return "4g"


def legacy_priority_to_mode(priority: str) -> str:
    if str(priority).strip().lower() == "wifi":
        return "wifi_only"
    return "prefer_4g"


def switch_target_to_mode(target: str) -> str:
    if str(target).strip().lower() == "wifi":
        return "wifi_only"
    return "prefer_4g"


def cloud_wifi_fallback_enabled(cfg) -> bool:
    network = cfg.data.get("network", {})
    value = network.get("cloud_wifi_fallback")
    if value is None:
        return True
    return bool(value)


def build_network_mode_payload(cfg) -> Dict[str, Any]:
    status = read_network_status()
    network = cfg.data.get("network", {})
    mode = network.get("mode") or "prefer_4g"
    return {
        "success": True,
        "mode": mode,
        "priority": mode_to_legacy_priority(mode),
        "cloud_wifi_fallback": cloud_wifi_fallback_enabled(cfg),
        "fallback_delay": int(network.get("fallback_delay", 300)),
        "active_interface": status.get("active_interface", ""),
        "route_reason": status.get("route_reason", ""),
        "network_type": status.get("network_type", ""),
        "physical_egress_iface": status.get("physical_egress_iface", ""),
        "physical_egress_ip": status.get("physical_egress_ip", ""),
    }


def apply_network_mode(
    cfg,
    mode: str,
    cloud_fallback: Optional[bool] = None,
    fallback_delay: Optional[int] = None,
) -> None:
    if mode not in VALID_MODES:
        raise ValueError("Invalid mode. Use: prefer_4g, 4g_only, wifi_only")

    network = cfg.data.setdefault("network", {})
    network["mode"] = mode
    if cloud_fallback is not None:
        network["cloud_wifi_fallback"] = bool(cloud_fallback)
    if fallback_delay is not None and fallback_delay >= 0:
        network["fallback_delay"] = int(fallback_delay)
    cfg.save()
    network_controller.set_priority(mode_to_legacy_priority(mode))
    _trigger_netmon_apply(mode)


def _trigger_netmon_apply(mode: str) -> None:
    def _run() -> None:
        try:
            result = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "dronebridge-netmon.service"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                logger.info("[WEB][NET] network.mode=%s → netmon restarted", mode)
                return
        except (subprocess.TimeoutExpired, OSError):
            pass

        time.sleep(0.2)
        script = module_4g_path("connection_manager.py")
        if not script.exists():
            logger.warning("[WEB][NET] network.mode=%s saved; Module_4G missing", mode)
            return
        try:
            network_controller.run_once()
            logger.info("[WEB][NET] network.mode=%s → connection_manager once OK", mode)
        except Exception as exc:
            logger.warning(
                "[WEB][NET] network.mode=%s saved; netmon will apply within ~30s (%s)",
                mode,
                exc,
            )

    threading.Thread(target=_run, daemon=True).start()
