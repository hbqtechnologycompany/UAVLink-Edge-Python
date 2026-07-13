"""WireGuard VPN manager — MAVLink uplink to 10.8.0.1 via tunnel (Pi_CM5 parity)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("VPN")

_INTERFACE = "uavlink0"


class VPNManager:
    def __init__(self, config):
        self.config = config
        vpn = config.data.get("vpn", {}) if hasattr(config, "data") else {}
        self.enabled = bool(vpn.get("enabled", True))
        self.config_file = Path(vpn.get("config_file") or "vpn_config.json")
        self.server_endpoint = vpn.get("server_endpoint") or "45.117.171.237:51820"
        self.router_vpn_ip = vpn.get("router_vpn_ip") or "10.8.0.1"
        self._lock = threading.RLock()
        self._running = False
        self._cfg: Optional[dict] = None

    def is_enabled(self) -> bool:
        return self.enabled

    def config_exists(self) -> bool:
        return self.config_file.exists()

    def needs_reprovision(self, drone_uuid: str) -> bool:
        """True nếu chưa có config hoặc config thuộc UUID khác (đổi drone)."""
        if not self.config_exists():
            return True
        cfg = self.load_config()
        if not cfg:
            return True
        stored = str(cfg.get("drone_uuid") or "").strip()
        if not stored:
            return True
        return stored != str(drone_uuid or "").strip()

    def invalidate_config(self) -> None:
        with self._lock:
            if self._running:
                self.stop()
            if self.config_file.exists():
                self.config_file.unlink()
                logger.info("[VPN] Removed stale %s", self.config_file)
            self._cfg = None

    def load_config(self) -> Optional[dict]:
        if not self.config_file.exists():
            return None
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
            self._cfg = data
            return data
        except json.JSONDecodeError as exc:
            logger.error("Invalid %s: %s", self.config_file, exc)
            return None

    def get_assigned_ip(self) -> str:
        cfg = self._cfg or self.load_config()
        if not cfg:
            return ""
        ip = str(cfg.get("assigned_ip") or "")
        if "/" in ip:
            ip = ip.split("/", 1)[0]
        return ip

    def is_running(self) -> bool:
        with self._lock:
            if self._running:
                return True
            return self._interface_up()

    def _interface_up(self) -> bool:
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", _INTERFACE],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return False
            match = re.search(r"inet (\S+)", result.stdout or "")
            if not match:
                return False
            ip = match.group(1).split("/", 1)[0]
            cfg = self._cfg or self.load_config() or {}
            if ip:
                cfg["assigned_ip"] = ip
                self._cfg = cfg
            return True
        except (subprocess.TimeoutExpired, OSError):
            return False

    def save_provisioned(
        self,
        private_key: str,
        public_key: str,
        assigned_ip: str,
        server_pub_key: str,
        server_endpoint: str,
        drone_uuid: str = "",
    ) -> None:
        payload = {
            "assigned_ip": assigned_ip,
            "private_key": private_key,
            "public_key": public_key,
            "server_pub_key": server_pub_key,
            "server_endpoint": server_endpoint,
            "provisioned_at": int(__import__("time").time()),
            "drone_uuid": drone_uuid,
        }
        self.config_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.config_file, 0o600)
        self._cfg = payload
        logger.info("[VPN] Saved %s (IP=%s)", self.config_file, assigned_ip)

    def load_or_generate_keypair(self) -> Tuple[str, str, bool]:
        cfg = self.load_config()
        if cfg and cfg.get("private_key") and cfg.get("public_key"):
            return cfg["private_key"], cfg["public_key"], False
        if not shutil.which("wg"):
            raise RuntimeError(
                "wireguard-tools chưa cài. Chạy: sudo apt install -y wireguard-tools"
            )
        priv = subprocess.check_output(["wg", "genkey"], text=True).strip()
        pub = subprocess.check_output(["wg", "pubkey"], input=priv, text=True).strip()
        return priv, pub, True

    def _write_quick_conf(self, cfg: dict) -> Path:
        ip = cfg["assigned_ip"]
        if "/" not in ip:
            ip = f"{ip}/32"
        conf = (
            "[Interface]\n"
            f"PrivateKey = {cfg['private_key']}\n"
            f"Address = {ip}\n\n"
            "[Peer]\n"
            f"PublicKey = {cfg['server_pub_key']}\n"
            f"Endpoint = {cfg['server_endpoint']}\n"
            f"AllowedIPs = {self.router_vpn_ip}/32, 10.8.0.0/24\n"
            "PersistentKeepalive = 15\n"
        )
        path = Path(tempfile.gettempdir()) / f"{_INTERFACE}.conf"
        path.write_text(conf, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def start(self) -> None:
        with self._lock:
            if self._running or self._interface_up():
                self._running = True
                logger.info(
                    "[VPN] Using existing %s — assigned %s",
                    _INTERFACE,
                    self.get_assigned_ip(),
                )
                return
            cfg = self.load_config()
            if not cfg:
                raise RuntimeError(f"VPN config not found: {self.config_file}")
            if not shutil.which("wg"):
                raise RuntimeError(
                    "wireguard-tools chưa cài. Chạy: sudo apt install -y wireguard-tools"
                )

            conf_path = self._write_quick_conf(cfg)
            if not shutil.which("wg-quick"):
                raise RuntimeError("wg-quick not found (install wireguard-tools)")

            if os.geteuid() == 0:
                cmd = ["wg-quick", "up", str(conf_path)]
            else:
                cmd = ["sudo", "-n", "wg-quick", "up", str(conf_path)]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                if "already exists" in err.lower() and self._interface_up():
                    self._running = True
                    logger.info(
                        "[VPN] Interface %s already up — assigned %s",
                        _INTERFACE,
                        self.get_assigned_ip(),
                    )
                    return
                raise RuntimeError(f"wg-quick up failed: {err}")

            self._running = True
            logger.info(
                "[VPN] Tunnel up on %s — assigned %s → %s",
                _INTERFACE,
                self.get_assigned_ip(),
                self.router_vpn_ip,
            )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            conf_path = Path(tempfile.gettempdir()) / f"{_INTERFACE}.conf"
            if conf_path.exists() and shutil.which("wg-quick"):
                if os.geteuid() == 0:
                    cmd = ["wg-quick", "down", str(conf_path)]
                else:
                    cmd = ["sudo", "-n", "wg-quick", "down", str(conf_path)]
                subprocess.run(cmd, capture_output=True, text=True)
            self._running = False
            logger.info("[VPN] Tunnel stopped")

    def ping_router(self) -> bool:
        if not self.is_running():
            return False
        if not shutil.which("ping"):
            return True
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", self.router_vpn_ip],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
