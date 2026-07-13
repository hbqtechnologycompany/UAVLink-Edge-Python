import os
import yaml


class Config:
    def __init__(self, filename="config.yaml"):
        if not os.path.exists(filename) and os.path.exists("../config.yaml"):
            filename = "../config.yaml"

        self.filename = filename
        with open(filename, "r", encoding="utf-8") as f:
            self.data = yaml.safe_load(f) or {}

        self._sync_network_aliases()
        self.log = self.data.get("log", {})
        self.auth = self.data.get("auth", {})
        self.network = self.data.get("network", {})
        self.mavlink = self.network  # backward-compatible alias
        self.forwarding = self.data.get("forwarding", {})
        self.web = self.data.get("web", {})
        self.video = self.data.get("video", {})
        self.camera = self.data.get("camera", {})
        self.ethernet = self.data.get("ethernet", {})
        self.landing = self.data.get("landing", {})
        self.lcd = self.data.get("lcd", {})
        self.vpn = self.data.get("vpn", {})

    def _sync_network_aliases(self):
        network = self.data.setdefault("network", {})
        mavlink = self.data.get("mavlink", {})

        for key, value in mavlink.items():
            network.setdefault(key, value)

        if "target_host" not in network and self.data.get("forwarding"):
            network.setdefault("target_host", self.data["forwarding"].get("target_host"))
            network.setdefault("target_port", self.data["forwarding"].get("target_port"))
            network.setdefault("protocol", self.data["forwarding"].get("protocol", "udp"))

        if "local_listen_port" not in network and "tcp_port" in network:
            network["local_listen_port"] = network["tcp_port"]

        self.data["network"] = network

    def save(self):
        with open(self.filename, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        self._sync_network_aliases()

    def get_address(self):
        host = self.forwarding.get("target_host") or self.network.get("target_host")
        port = self.forwarding.get("target_port") or self.network.get("target_port")
        return f"{host}:{port}"

    def get_network_config_for_api(self):
        return {
            "connection_type": self.network.get("connection_type", "serial"),
            "serial_port": self.network.get("serial_port", "/dev/ttyAMA0"),
            "serial_baud": self.network.get("serial_baud", 57600),
            "local_listen_port": self.network.get("local_listen_port", self.network.get("tcp_port", 14540)),
            "target_host": self.network.get("target_host"),
            "target_port": self.network.get("target_port"),
            "protocol": self.network.get("protocol", "udp"),
            "mode": self.network.get("mode", "prefer_4g"),
            "cloud_wifi_fallback": self.network.get("cloud_wifi_fallback", True),
            "forward_gps_raw_int": self.network.get("forward_gps_raw_int", True),
            "fallback_delay": self.network.get("fallback_delay", 300),
        }
