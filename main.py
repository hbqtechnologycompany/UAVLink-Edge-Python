#!/usr/bin/env python3
"""UAVLink-Edge entry point."""
import os
import sys
from pathlib import Path


def _reexec_with_venv_python() -> None:
    """sudo python main.py → dùng venv/bin/python (tránh thiếu pymavlink)."""
    if os.environ.get("UAVLINK_VENV_REEXEC") == "1":
        return
    root = Path(__file__).resolve().parent
    venv_python = root / "venv" / "bin" / "python3"
    if not venv_python.is_file():
        venv_python = root / "venv" / "bin" / "python"
    if not venv_python.is_file():
        return
    venv_root = str(venv_python.parent.parent)
    # Debian venv: python3 → /usr/bin/python3; so compare prefix, not executable path.
    if sys.prefix == venv_root or os.environ.get("VIRTUAL_ENV") == venv_root:
        return
    env = os.environ.copy()
    env["UAVLINK_VENV_REEXEC"] = "1"
    env["VIRTUAL_ENV"] = venv_root
    os.execve(str(venv_python), [str(venv_python), *sys.argv], env)


_reexec_with_venv_python()

import argparse
import logging
import signal
import threading
import time

from logging_setup import setup_logging

setup_logging()

from config import Config
from auth_client import AuthClient
from forwarder import Forwarder
from web_server import start_server
from video_streamer import VideoStreamer
from metrics import global_metrics
from network_controller import start_network_monitor
from vpn_manager import VPNManager
from cloud_egress import wait_for_cloud_egress
from ethernet_setup import ensure_ethernet_ready, start_ethernet_watchdog
from instance_lock import acquire_instance_lock
from camera_mavlink import start_camera_mavlink_bridge
from landing_mavlink import start_landing_mavlink_bridge

logger = logging.getLogger("MAIN")

video_streamer = None
vpn_manager = None
_stop_event = threading.Event()


def signal_handler(sig, frame):
    logger.info("Shutting down...")
    _stop_event.set()
    if video_streamer:
        video_streamer.stop()
    if vpn_manager:
        try:
            vpn_manager.stop()
        except Exception:
            pass
    sys.exit(0)


def main():
    global video_streamer, vpn_manager

    parser = argparse.ArgumentParser(description="UAVLink-Edge (Python Version)")
    parser.add_argument("--register", action="store_true", help="Register drone with server to get SecretKey")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if not args.register:
        acquire_instance_lock()

    logger.info("Starting UAVLink-Edge (Python Version) on Pi 5")
    global_metrics.add_log("INFO", "UAVLink-Edge Python starting")

    try:
        cfg = Config("config.yaml")
        logger.info("Configuration loaded successfully")
    except Exception as e:
        logger.fatal(f"Failed to load configuration: {e}")
        sys.exit(1)

    auth = AuthClient(
        cfg.auth.get("host"),
        cfg.auth.get("port"),
        cfg.auth.get("uuid"),
        cfg.auth.get("shared_secret"),
        cfg.auth.get("keepalive_interval", 30),
    )
    auth.set_registration_meta(
        int(cfg.auth.get("vehicle_type", 0) or 0),
        str(cfg.auth.get("model", "") or ""),
    )

    vpn_manager = VPNManager(cfg)
    drone_uuid = str(cfg.auth.get("uuid") or "")

    if args.register:
        logger.info("Registering drone UUID=%s ...", drone_uuid)
        if auth.register():
            logger.info("Registration successful. Chạy lại: python main.py (không --register)")
        else:
            logger.error("Registration failed")
        sys.exit(0)

    if not auth.load_secret():
        logger.fatal(
            "Chưa đăng ký hoặc UUID/secret không khớp config (%s). Chạy: python main.py --register",
            drone_uuid,
        )
        sys.exit(1)

    if vpn_manager.is_enabled():
        if vpn_manager.needs_reprovision(drone_uuid):
            logger.warning(
                "[VPN] vpn_config.json không khớp UUID %s — xóa và provision lại sau auth",
                drone_uuid,
            )
            vpn_manager.invalidate_config()
        elif vpn_manager.config_exists() or vpn_manager.is_running():
            try:
                vpn_manager.start()
                logger.info("[VPN] Tunnel ready — assigned %s", vpn_manager.get_assigned_ip())
            except Exception as exc:
                logger.warning("[VPN] Could not start existing tunnel: %s", exc)

    fwd = Forwarder(cfg, auth, vpn_manager=vpn_manager)
    start_server(cfg.web.get("port", 8080), fwd.stats, auth, forwarder=fwd, cfg=cfg)
    start_network_monitor()

    # Eth + MAVLink trước cloud auth — Pixhawk/GCS local không chờ VPN.
    if not ensure_ethernet_ready(cfg):
        logger.warning("[STARTUP] Ethernet not ready — forwarder will retry serial if configured")
    start_ethernet_watchdog(cfg, _stop_event)

    if not fwd.start():
        logger.fatal("Failed to start forwarder")
        sys.exit(1)

    start_camera_mavlink_bridge(cfg, fwd, _stop_event)
    start_landing_mavlink_bridge(cfg, fwd, _stop_event)
    logger.info("[STARTUP] MAVLink forwarder active (cloud auth/VPN may still be initializing)")

    iface, ip, ready = wait_for_cloud_egress(120.0)
    if ready:
        logger.info("[STARTUP] Cloud egress ready: %s (%s)", iface, ip)
    else:
        logger.warning("[STARTUP] cloud_ready not confirmed — auth will retry until uplink is OK")

    logger.info("[STARTUP] Authenticating with router server (auth before VPN)...")
    if not auth.start():
        logger.warning("Initial authentication failed. Will retry in background.")
    else:
        logger.info("Successfully authenticated")

    if vpn_manager.is_enabled():
        if not vpn_manager.config_exists():
            logger.info("[VPN] Requesting WireGuard provisioning for UUID=%s ...", drone_uuid)
            if auth.request_vpn_provision(vpn_manager):
                try:
                    vpn_manager.start()
                    logger.info("[VPN] Tunnel up — %s (UUID=%s)", vpn_manager.get_assigned_ip(), drone_uuid)
                except Exception as exc:
                    logger.error("[VPN] wg-quick failed: %s", exc)
            else:
                logger.error("[VPN] Provision failed — MAVLink sẽ không lên server")
        elif not vpn_manager.is_running():
            try:
                vpn_manager.start()
            except Exception as exc:
                logger.error("[VPN] Could not start tunnel: %s", exc)
        if vpn_manager.is_running():
            fwd.rebind_vpn_socket()
            if vpn_manager.ping_router():
                logger.info("[VPN] Router %s reachable", vpn_manager.router_vpn_ip)
            else:
                logger.warning("[VPN] Cannot ping router %s", vpn_manager.router_vpn_ip)

    if cfg.camera.get("enabled") and cfg.camera.get("auto_start"):
        try:
            from web.camera_service import camera_restart

            result, code = camera_restart(cfg)
            if result.get("success"):
                logger.info("[CAMERA] Auto-start OK: %s", result.get("message", "started"))
            else:
                logger.warning("[CAMERA] Auto-start failed: %s", result.get("message"))
        except Exception as exc:
            logger.warning("[CAMERA] Auto-start error: %s", exc)

    video_cfg = cfg.video if hasattr(cfg, "video") else {}
    if str(video_cfg.get("source", "picamera")).lower() not in ("none", "off", "disabled"):
        video_streamer = VideoStreamer(cfg)
        video_streamer.start()
    else:
        logger.info("Video streamer disabled (video.source=%s)", video_cfg.get("source"))

    logger.info("UAVLink-Edge running. Press Ctrl+C to stop.")

    while not _stop_event.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
