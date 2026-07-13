#!/usr/bin/env python3
"""
DroneBridge Network Monitor — Policy-Based Routing (PBR) Mode
=============================================================
Trách nhiệm (3 mặt phẳng — xem docs/INTERFACE_POLICY.md):
  - eth0  : MAVLink PX4 LAN — không PBR mark, table 103 eth_pixhawk
  - wlan0 : Admin (SSH, dashboard reply) — main table + table 101 wlan_reply
  - wwan0 : Cloud uplink (auth/VPN/MAVLink server) — table 100 dronebridge + fwmark
  - Monitor 4G/WiFi và trigger reinit 4G qua systemd khi cần

Yêu cầu:
  - /etc/iproute2/rt_tables phải có dòng "100 dronebridge"
  - setup_pbr.sh phải chạy trước (qua ExecStartPre trong systemd)
  - Chạy với quyền root (sudo)

Tác giả: DroneBridge Team
Phiên bản: 2.0 (PBR, không dùng metric)
"""

import subprocess
import time
import json
import re
import os
import sys
import logging
import socket
from datetime import datetime


# ─── Cấu hình ───────────────────────────────────────────────────────────────
PBR_TABLE       = 100        # Cloud data plane (auth/VPN/MAVLink uplink)
PBR_TABLE_NAME  = "dronebridge"
WLAN_REPLY_TABLE = 101       # Reply từ source IP wlan0
WLAN_REPLY_NAME  = "wlan_reply"
ETH_PIXHAWK_TABLE = 103      # MAVLink PX4 trên eth0 — không qua 4G
ETH_PIXHAWK_NAME  = "eth_pixhawk"
WLAN_REPLY_RULE_PRIO = 50
ETH_PIXHAWK_RULE_PRIO = 45
FWMARK          = "0x1"      # Packet mark tương ứng với ip rule (normalized)
FWMARK_MASK     = "0x1/0x1"  # Match bit 0, ignore other mark bits
IPTABLES_MARK     = "0x1"      # iptables -m mark (khác FWMARK_MASK dùng cho ip rule)
DRONEBRIDGE_USER = os.getenv("DRONEBRIDGE_USER", "cm5drone6425")

PING_HOST           = "8.8.8.8"
MONITOR_INTERVAL_S  = 30     # Giây giữa các lần kiểm tra
FAIL_THRESHOLD      = 1      # Drone đang bay: phản ứng nhanh khi 4G mất
REINIT_TIMEOUT_S    = 120    # Giây tối đa chờ 4G phục hồi sau reinit
REINIT_RETRY_MIN    = 5      # Phút tối thiểu giữa 2 lần reinit
REINIT_RETRY_MIN_WIFI = 1    # Khi có WiFi fallback, retry 4G sớm (phút)
REINIT_RETRY_MAX_WIFI = 2    # Trần backoff khi đang fallback WiFi (phút)

try:
    REINIT_RETRY_MIN_WIFI = int(os.getenv("DRONEBRIDGE_REINIT_RETRY_MIN_WIFI", str(REINIT_RETRY_MIN_WIFI)))
except Exception:
    REINIT_RETRY_MIN_WIFI = 1

try:
    REINIT_RETRY_MAX_WIFI = int(os.getenv("DRONEBRIDGE_REINIT_RETRY_MAX_WIFI", str(REINIT_RETRY_MAX_WIFI)))
except Exception:
    REINIT_RETRY_MAX_WIFI = 2

SYSTEMD_4G_SERVICE  = "dronebridge-4g-init.service"
STATUS_FILE         = "/run/dronebridge/network_status.json"
LOG_DIR             = os.getenv("DRONEBRIDGE_LOG_DIR", "/home/pi/Run_serverGo/logs")
EVENT_LOG_FILE      = os.path.join(LOG_DIR, "4g_link_events.log")
EGRESS_LOG_FILE     = os.path.join(LOG_DIR, "egress_path.log")

WG_COUNTER_DPORT    = os.getenv("DRONEBRIDGE_WG_PORT", "51820")
MAVLINK_TARGET_HOST = os.getenv("DRONEBRIDGE_MAVLINK_TARGET_HOST", "10.8.0.1")
MAVLINK_TARGET_PORT = os.getenv("DRONEBRIDGE_MAVLINK_TARGET_PORT", "14550")
DRONEBRIDGE_UID     = os.getenv("DRONEBRIDGE_UID", "1000")
HEALTH_TCP_HOST     = os.getenv("DRONEBRIDGE_HEALTH_TCP_HOST", "45.117.171.237")
HEALTH_TCP_PORT     = int(os.getenv("DRONEBRIDGE_HEALTH_TCP_PORT", "5770"))
HEALTH_TCP_FALLBACK_HOST = os.getenv("DRONEBRIDGE_HEALTH_TCP_FALLBACK_HOST", "1.1.1.1")
HEALTH_TCP_FALLBACK_PORT = int(os.getenv("DRONEBRIDGE_HEALTH_TCP_FALLBACK_PORT", "443"))
FORCE_4G_ONLY_ENV   = os.getenv("DRONEBRIDGE_FORCE_4G_ONLY")  # optional override
DRONEBRIDGE_SERVICE = os.getenv("DRONEBRIDGE_SERVICE_NAME", "dronebridge")

# ─── Config network.mode (align UAVLink-Edge / config.yaml) ───────────────────
def _config_paths():
    seen = set()
    for path in (
        os.getenv("DRONEBRIDGE_CONFIG", ""),
        "/opt/dronebridge/config.yaml",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"),
        "config.yaml",
    ):
        if path and path not in seen and os.path.isfile(path):
            seen.add(path)
            yield path


def load_network_mode() -> str:
    """Read network.mode from config.yaml (prefer_4g | 4g_only | wifi_only)."""
    in_network = False
    for path in _config_paths():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if re.match(r"^network:\s*$", line):
                        in_network = True
                        continue
                    if in_network and line and not line[0].isspace():
                        in_network = False
                    if in_network:
                        m = re.match(r"^\s+mode:\s*(\S+)", line)
                        if m:
                            return m.group(1).strip('"').strip("'")
        except OSError:
            continue
    return "prefer_4g"


def load_fallback_delay() -> int:
    """Read network.fallback_delay from config.yaml (seconds, default 300)."""
    in_network = False
    for path in _config_paths():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if re.match(r"^network:\s*$", line):
                        in_network = True
                        continue
                    if in_network and line and not line[0].isspace():
                        in_network = False
                    if in_network:
                        m = re.match(r"^\s+fallback_delay:\s*(\d+)", line)
                        if m:
                            return int(m.group(1))
        except OSError:
            continue
    return 300


def load_cloud_wifi_fallback() -> bool:
    """Cho phép table 100 fallback sang wlan0 khi 4G down (default: true)."""
    env = os.getenv("DRONEBRIDGE_CLOUD_WIFI_FALLBACK")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off")
    in_network = False
    for path in _config_paths():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if re.match(r"^network:\s*$", line):
                        in_network = True
                        continue
                    if in_network and line and not line[0].isspace():
                        in_network = False
                    if in_network:
                        m = re.match(r"^\s+cloud_wifi_fallback:\s*(\S+)", line)
                        if m:
                            v = m.group(1).strip('"').strip("'").lower()
                            return v in ("1", "true", "yes", "on")
        except OSError:
            continue
    return True


def wlan_radio_blocked() -> bool:
    """True khi WiFi bị rfkill block — không dùng cho cloud probe/fallback."""
    _, out, _ = run(["rfkill", "list", "wifi"], timeout=3)
    if not out:
        return False
    return "Soft blocked: yes" in out or "Hard blocked: yes" in out


def force_4g_only_from_policy() -> bool:
    """True only when app must stay on wwan0 even if probe fails (4g_only)."""
    if FORCE_4G_ONLY_ENV is not None:
        return FORCE_4G_ONLY_ENV.strip().lower() not in ("0", "false", "no", "off")
    return load_network_mode() == "4g_only"


def _resolve_eth_iface(configured: str) -> str:
    """CM5: end0; Pi4: eth0 — chỉ dùng trong khối netmon (đọc config, không gán IP)."""
    if configured and os.path.exists(f"/sys/class/net/{configured}"):
        return configured
    for name in ("end0", "eth0"):
        if os.path.exists(f"/sys/class/net/{name}"):
            return name
    return configured or "eth0"


def load_ethernet_lan() -> dict:
    """Đọc ethernet.* từ config.yaml — subnet PX4 (eth plane)."""
    cfg = {
        "interface": os.getenv("DRONEBRIDGE_ETH_IFACE", "eth0"),
        "local_ip": "",
        "pixhawk_ip": "10.41.10.2",
        "subnet": "24",
    }
    in_eth = False
    for path in _config_paths():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if re.match(r"^ethernet:\s*$", line):
                        in_eth = True
                        continue
                    if in_eth and line and not line[0].isspace():
                        break
                    if not in_eth:
                        continue
                    for key, pat in (
                        ("interface", r"^\s+interface:\s*(\S+)"),
                        ("local_ip", r'^\s+local_ip:\s*"?([^"\s]+)"?'),
                        ("pixhawk_ip", r'^\s+pixhawk_ip:\s*"?([^"\s]+)"?'),
                        ("subnet", r'^\s+subnet:\s*"?(\d+)"?'),
                    ):
                        m = re.match(pat, line)
                        if m:
                            cfg[key] = m.group(1).strip("'")
        except OSError:
            continue

    cidr = os.getenv("DRONEBRIDGE_ETH_LAN_CIDR", "").strip()
    if not cidr and cfg["local_ip"]:
        parts = cfg["local_ip"].split(".")
        if len(parts) == 4:
            cidr = f"{parts[0]}.{parts[1]}.{parts[2]}.0/{cfg['subnet'] or '24'}"
    if not cidr:
        cidr = "10.41.10.0/24"
    cfg["cidr"] = cidr
    cfg["interface"] = _resolve_eth_iface(cfg.get("interface", "eth0"))
    return cfg

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("netmon")


# ─── Helpers ─────────────────────────────────────────────────────────────────
def run(cmd, timeout=10, check=False):
    """Chạy lệnh, trả về (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def run_root(cmd, timeout=10):
    return run(["sudo"] + cmd, timeout=timeout)


# ─── NetworkMonitor ────────────────────────────────────────────────────────────
class NetworkMonitor:
    def __init__(self):
        self._4g_failures  = 0
        self._last_reinit  = 0.0
        self._reinit_count = 0  # Exponential tracking
        self._wwan_snat_ip = None
        self._active_iface = None
        # Non-blocking reinit tracking
        self._reinit_pending     = False  # True khi đang chờ 4G-init service phục hồi
        self._reinit_pending_t   = 0.0   # Thời điểm bắt đầu chờ
        self._last_udp_bytes_wwan0 = None
        self._last_udp_sample_ts = None
        self._last_mavlink_total = None
        self._last_mavlink_sample_ts = None
        self._wwan_probe_mode = "INIT"
        self._4g_down_since = 0.0
        self._main_default_iface = "wlan0"
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)

    def _resolve_pbr_target(self, network_mode: str, wwan_ok: bool, wlan_ok: bool, force_4g_only: bool) -> tuple[str, str]:
        """
        Chọn interface cho table 100 — theo SYSTEM_ARCHITECTURE.md §5.2.
        Go app KHÔNG quyết định interface; chỉ đọc network_status.json.
        """
        if network_mode == "wifi_only":
            return "wlan0", "WIFI_ONLY"
        if force_4g_only or network_mode == "4g_only":
            return "wwan0", "4G_ONLY_HOLD_WWAN"

        if wwan_ok:
            self._4g_down_since = 0
            return "wwan0", "4G_PROBE_OK"

        now = time.time()
        if not self._4g_down_since:
            self._4g_down_since = now
        elapsed = now - self._4g_down_since
        delay = load_fallback_delay()

        if elapsed <= delay:
            return "wwan0", "4G_WAITING"

        if wlan_ok and load_cloud_wifi_fallback():
            return "wlan0", "WIFI_FALLBACK"

        if wlan_ok and not load_cloud_wifi_fallback():
            return "wwan0", "4G_WIFI_FALLBACK_LOCKED"

        return "wwan0", "4G_DOWN_NO_WIFI"

    @staticmethod
    def _format_bandwidth(bps: float) -> str:
        """Format tốc độ băng thông theo đơn vị dễ đọc (decimal)."""
        if bps >= 1_000_000_000:
            return f"{bps / 1_000_000_000:.2f}Gbps"
        if bps >= 1_000_000:
            return f"{bps / 1_000_000:.2f}Mbps"
        if bps >= 1_000:
            return f"{bps / 1_000:.2f}Kbps"
        return f"{bps:.0f}bps"

    def log_link_event(self, wwan_ip, wlan_ip, wwan_ok, wlan_ok):
        """Ghi log trạng thái link định kỳ với timestamp rõ ràng."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sig = self.get_4g_signal_metrics() if wwan_ip else {
            "quality": "N/A", "rssi": "N/A", "rsrp": "N/A", "snr": "N/A"
        }
        line = (
            f"{ts} | 4G_state={'UP' if wwan_ok else 'DOWN'} 4G_ip={wwan_ip or 'none'} 4G_ping={'OK' if wwan_ok else 'FAIL'} "
            f"4G_probe={self._wwan_probe_mode} "
            f"4G_signal={sig['quality']} rssi={sig['rssi']} rsrp={sig['rsrp']} snr={sig['snr']} "
            f"| WiFi_state={'UP' if wlan_ok else 'DOWN'} WiFi_ip={wlan_ip or 'none'} WiFi_ping={'OK' if wlan_ok else 'FAIL'} "
            f"| active={self._active_iface or 'none'}"
        )
        try:
            with open(EVENT_LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception as e:
            log.warning(f"Không ghi được event log file: {e}")

    def get_udp_wwan0_counter(self):
        """Lấy counter UDP transport đi ra wwan0 (dport WireGuard)."""
        rc, out, err = run_root(["iptables", "-w", "-nvx", "-L", "OUTPUT"], timeout=8)
        if rc != 0 or not out:
            return None, None, err or "iptables output rỗng"

        for line in out.splitlines():
            if (
                "ACCEPT" in line
                and f"owner UID match {DRONEBRIDGE_UID}" in line
                and "wwan0" in line
                and f"udp dpt:{WG_COUNTER_DPORT}" in line
            ):
                m = re.match(r"\s*(\d+)\s+(\d+)\s+", line)
                if m:
                    return int(m.group(1)), int(m.group(2)), None
                return None, None, f"parse lỗi: {line.strip()}"

        return None, None, f"không tìm thấy rule counter udp wwan0 dpt:{WG_COUNTER_DPORT} (uid={DRONEBRIDGE_UID})"

    def get_mavlink_forwarded_total(self):
        """Đọc tổng số MAVLink message đã forward từ log [STATS] của dronebridge."""
        rc, out, err = run_root(
            ["journalctl", "-u", DRONEBRIDGE_SERVICE, "-n", "200", "--no-pager"],
            timeout=8,
        )
        if rc != 0 or not out:
            return None, err or "journal output rỗng"

        for line in reversed(out.splitlines()):
            m = re.search(r"\[STATS\]\s+Forwarded\s+(\d+)\s+messages", line)
            if m:
                return int(m.group(1)), None

        return None, "không tìm thấy [STATS] Forwarded trong journal"

    def log_udp_wwan0_counter(self):
        """Ghi log băng thông UDP out qua wwan0 ở dạng đơn giản."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sample_ts = time.time()
        pkts, byts, err = self.get_udp_wwan0_counter()
        mav_total, mav_err = self.get_mavlink_forwarded_total()

        if mav_total is None:
            mav_total_text = "N/A"
            mav_rate_text = "N/A"
        else:
            if self._last_mavlink_total is None or self._last_mavlink_sample_ts is None:
                mav_rate = 0.0
            else:
                elapsed_mav = max(0.001, sample_ts - self._last_mavlink_sample_ts)
                delta_mav = max(0, mav_total - self._last_mavlink_total)
                mav_rate = delta_mav / elapsed_mav

            self._last_mavlink_total = mav_total
            self._last_mavlink_sample_ts = sample_ts
            mav_total_text = str(mav_total)
            mav_rate_text = f"{mav_rate:.2f}"

        if pkts is None or byts is None:
            line = (
                f"{ts} | udp_out_wwan0_dport{WG_COUNTER_DPORT} packets_total=N/A bytes_total_kb=N/A "
                f"interval_s=N/A bandwidth=N/A "
                f"mavlink_msg_total={mav_total_text} mavlink_msg_rate={mav_rate_text} "
                f"active={self._active_iface or 'none'} error={err or 'unknown'}"
            )
            if mav_err:
                line += f" mav_err={mav_err}"
        else:
            if self._last_udp_bytes_wwan0 is None:
                delta_byts = 0
                elapsed_s = 0.0
            else:
                delta_byts = max(0, byts - self._last_udp_bytes_wwan0)
                if self._last_udp_sample_ts is None:
                    elapsed_s = float(MONITOR_INTERVAL_S)
                else:
                    elapsed_s = max(0.001, sample_ts - self._last_udp_sample_ts)

            bandwidth_bps = ((delta_byts * 8.0) / elapsed_s) if elapsed_s > 0 else 0.0
            bandwidth_human = self._format_bandwidth(bandwidth_bps)
            total_kb = byts / 1024.0

            self._last_udp_bytes_wwan0 = byts
            self._last_udp_sample_ts = sample_ts

            line = (
                f"{ts} | udp_out_wwan0_dport{WG_COUNTER_DPORT} packets_total={pkts} bytes_total_kb={total_kb:.1f} "
                f"interval_s={elapsed_s:.1f} bandwidth={bandwidth_human} "
                f"mavlink_msg_total={mav_total_text} mavlink_msg_rate={mav_rate_text} "
                f"active={self._active_iface or 'none'}"
            )
            if mav_err:
                line += f" mav_err={mav_err}"

        try:
            with open(EGRESS_LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception as e:
            log.warning(f"Không ghi được egress log file: {e}")

    def get_4g_signal_metrics(self):
        """Lấy chất lượng sóng 4G từ qmicli (nếu khả dụng)."""
        rc, out, _ = run(["qmicli", "-d", "/dev/cdc-wdm0", "--nas-get-signal-strength"], timeout=8)
        if rc != 0 or not out:
            return {"quality": "UNKNOWN", "rssi": "N/A", "rsrp": "N/A", "snr": "N/A"}

        rssi_match = re.search(r"RSSI:\s*(?:\n\s*Network\s+'[^']+':\s*)?'(-?\d+(?:\.\d+)?)\s*dBm'", out)
        rsrp_match = re.search(r"RSRP:\s*(?:\n\s*Network\s+'[^']+':\s*)?'(-?\d+(?:\.\d+)?)\s*dBm'", out)
        snr_match = re.search(r"SNR:\s*(?:\n\s*Network\s+'[^']+':\s*)?'(-?\d+(?:\.\d+)?)\s*dB'", out)

        rssi = int(float(rssi_match.group(1))) if rssi_match else None
        rsrp = int(float(rsrp_match.group(1))) if rsrp_match else None
        snr = float(snr_match.group(1)) if snr_match else None

        quality = "UNKNOWN"
        if rsrp is not None:
            if rsrp >= -90:
                quality = "STRONG"
            elif rsrp >= -100:
                quality = "GOOD"
            elif rsrp >= -110:
                quality = "FAIR"
            else:
                quality = "WEAK"
        elif rssi is not None:
            if rssi >= -70:
                quality = "STRONG"
            elif rssi >= -85:
                quality = "GOOD"
            elif rssi >= -100:
                quality = "FAIR"
            else:
                quality = "WEAK"

        return {
            "quality": quality,
            "rssi": f"{rssi}dBm" if rssi is not None else "N/A",
            "rsrp": f"{rsrp}dBm" if rsrp is not None else "N/A",
            "snr": f"{snr}dB" if snr is not None else "N/A",
            "signal_dbm": rsrp if rsrp is not None else rssi,
        }

    def get_wifi_signal_dbm(self) -> int | None:
        """Sóng WiFi admin (wlan0) — chỉ netmon đo, consumer đọc network_status.json."""
        for cmd in (["iw", "dev", "wlan0", "link"], ["iwconfig", "wlan0"]):
            rc, out, _ = run(cmd, timeout=3)
            if rc != 0 or not out:
                continue
            m = re.search(r"signal:\s*(-?\d+)", out) or re.search(
                r"Signal level[=:](-?\d+)", out
            )
            if m:
                return int(m.group(1))
        return None

    # ── Interface helpers ────────────────────────────────────────────────────
    def get_iface_ip(self, iface: str) -> str | None:
        """Trả về IP nếu interface UP và có địa chỉ IPv4, ngược lại None."""
        rc, out, _ = run(["ip", "-4", "addr", "show", iface])
        if rc != 0 or not out:
            return None
        # Chấp nhận UP, UNKNOWN (tunnel/raw-IP drivers như wwan0)
        if "state UP" not in out and "state UNKNOWN" not in out:
            if ",UP>" not in out and "<UP," not in out:
                return None
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None

    def ping_via(self, iface: str, host=PING_HOST, count=2) -> bool:
        """Ping qua một interface cụ thể."""
        # Với wwan0 (raw-IP), main table thường không có default qua 4G.
        # Thêm route host tạm để tránh false-negative khi kiểm tra ping.
        if iface == "wwan0":
            route_added = False
            run_root(["ip", "route", "del", f"{host}/32", "dev", "wwan0"], timeout=3)
            rc_add, _, _ = run_root(["ip", "route", "add", f"{host}/32", "dev", "wwan0"], timeout=4)
            route_added = (rc_add == 0)
            rc, _, _ = run(["ping", "-c", str(count), "-W", "3", "-I", iface, host], timeout=12)
            if route_added:
                run_root(["ip", "route", "del", f"{host}/32", "dev", "wwan0"], timeout=3)
            return rc == 0

        rc, _, _ = run(["ping", "-c", str(count), "-W", "3", "-I", iface, host], timeout=12)
        return rc == 0

    def tcp_probe_via_wwan(self, wwan_ip: str, host: str = HEALTH_TCP_HOST, port: int = HEALTH_TCP_PORT, timeout_s: int = 4) -> bool:
        """Fallback health-check 4G: TCP qua wwan0 (main table /32 tạm)."""
        if not wwan_ip:
            return False

        def probe_one(target_host: str, target_port: int) -> bool:
            route_added = False
            try:
                run_root(["ip", "route", "del", f"{target_host}/32", "dev", "wwan0"], timeout=3)
                rc_add, _, _ = run_root(
                    ["ip", "route", "add", f"{target_host}/32", "dev", "wwan0"], timeout=4,
                )
                route_added = (rc_add == 0)
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout_s)
                s.connect((target_host, target_port))
                s.close()
                return True
            except Exception:
                return False
            finally:
                if route_added:
                    run_root(["ip", "route", "del", f"{target_host}/32", "dev", "wwan0"], timeout=3)

        if probe_one(host, port):
            return True

        if HEALTH_TCP_FALLBACK_HOST and (
            HEALTH_TCP_FALLBACK_HOST != host or HEALTH_TCP_FALLBACK_PORT != port
        ):
            return probe_one(HEALTH_TCP_FALLBACK_HOST, HEALTH_TCP_FALLBACK_PORT)
        return False

    def get_wwan_gateway(self) -> str | None:
        """Lấy peer/gateway của wwan0 từ 'ip addr show'."""
        _, out, _ = run(["ip", "-4", "addr", "show", "wwan0"])
        m = re.search(r"peer (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None

    def get_wifi_gateway(self) -> str | None:
        """Lấy gateway của wlan0 từ route table."""
        _, out, _ = run(["ip", "route", "show", "dev", "wlan0"])
        m = re.search(r"via (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
        # Fallback: tính từ IP theo quy ước .1
        ip = self.get_iface_ip("wlan0")
        if ip:
            return ip.rsplit(".", 1)[0] + ".1"
        return None

    # ── 3-plane policy (eth / wlan admin / wwan data) ─────────────────────────
    @staticmethod
    def _ip_rule_exists(match_fragment: str) -> bool:
        _, out, _ = run(["ip", "rule", "show"])
        return match_fragment in out

    def ensure_pbr_eth_lan_route(self):
        """Route PX4 LAN trong table 100 — an toàn khi packet vẫn bị mark."""
        eth = load_ethernet_lan()
        cidr = eth.get("cidr")
        iface = eth.get("interface", "eth0")
        if not cidr or not self.get_iface_ip(iface):
            return False
        rc, _, err = run_root([
            "ip", "route", "replace", cidr, "dev", iface,
            "table", str(PBR_TABLE),
        ])
        if rc != 0:
            log.warning(f"[PBR] eth LAN route table {PBR_TABLE} failed: {err}")
            return False
        return True

    def ensure_eth_pixhawk_plane(self) -> dict:
        """Table 103 + ip rule from eth IP — MAVLink CM5↔PX4 luôn qua netdev PX4."""
        eth = load_ethernet_lan()
        iface = eth["interface"]
        cidr = eth["cidr"]
        local_ip = self.get_iface_ip(iface)
        link_up = bool(local_ip)

        if link_up:
            run_root([
                "ip", "route", "replace", cidr, "dev", iface,
                "table", str(ETH_PIXHAWK_TABLE),
            ])
            rule_key = f"from {local_ip}/32 lookup {ETH_PIXHAWK_NAME}"
            if not self._ip_rule_exists(rule_key):
                run_root([
                    "ip", "rule", "add", "from", f"{local_ip}/32",
                    "table", str(ETH_PIXHAWK_TABLE),
                    "priority", str(ETH_PIXHAWK_RULE_PRIO),
                ])

        return {
            "interface": iface,
            "cidr": cidr,
            "ip": local_ip or None,
            "pixhawk_ip": eth.get("pixhawk_ip"),
            "link_up": link_up,
        }

    def ensure_wlan_admin_reply(self) -> dict:
        """Table 101 + ip rule from wlan IP — reply SSH/dashboard đúng wlan0."""
        wlan_ip = self.get_iface_ip("wlan0")
        gw = self.get_wifi_gateway()
        rule_ok = False
        if wlan_ip and gw:
            run_root([
                "ip", "route", "replace", "default", "via", gw, "dev", "wlan0",
                "table", str(WLAN_REPLY_TABLE),
            ])
            rule_key = f"from {wlan_ip}/32 lookup {WLAN_REPLY_NAME}"
            if not self._ip_rule_exists(rule_key):
                run_root([
                    "ip", "rule", "add", "from", f"{wlan_ip}/32",
                    "table", str(WLAN_REPLY_TABLE),
                    "priority", str(WLAN_REPLY_RULE_PRIO),
                ])
            rule_ok = True
        return {"ip": wlan_ip, "reply_rule": "ok" if rule_ok else "missing"}

    def ensure_local_planes(self) -> tuple[dict, dict]:
        """Đồng bộ wlan admin reply (table 101) + eth PX4 — không đụng main default."""
        wlan = self.ensure_wlan_admin_reply()
        eth = self.ensure_eth_pixhawk_plane()
        return wlan, eth

    def ensure_admin_plane(self) -> tuple[dict, dict]:
        """Đồng bộ wlan admin + eth PX4 — gọi mỗi chu kỳ netmon."""
        wlan, eth = self.ensure_local_planes()
        self.ensure_system_wifi_route()
        return wlan, eth

    def ensure_system_default_route(self, wwan_ok: bool, network_mode: str = "prefer_4g") -> str:
        """
        Main table default:
        - cloud_wifi_fallback=false + 4G up → wwan0 (ffmpeg, curl, … không mark)
        - còn lại → wlan0 (legacy / WiFi fallback)
        SSH + dashboard: inbound wlan0 + table 101 from wlan IP — không cần main default wlan.
        """
        prefer_wwan_main = (
            network_mode != "wifi_only"
            and not load_cloud_wifi_fallback()
            and wwan_ok
            and os.path.exists("/sys/class/net/wwan0")
        )
        if prefer_wwan_main:
            wwan_ip = self.get_iface_ip("wwan0")
            if wwan_ip:
                run_root(["ip", "route", "del", "default", "dev", "wlan0"])
                _, out, _ = run(["ip", "route", "show", "default", "dev", "wwan0"])
                if "default" not in out:
                    rc, _, err = run_root([
                        "ip", "route", "replace", "default", "dev", "wwan0",
                        "src", wwan_ip,
                    ])
                    if rc == 0:
                        log.info(
                            f"[SYS_ROUTE] Main default → wwan0 ({wwan_ip}); "
                            "WiFi chỉ admin (SSH, dashboard)"
                        )
                    else:
                        log.warning(f"[SYS_ROUTE] Không set main default wwan0: {err}")
                return "wwan0"

        self.ensure_system_wifi_route()
        return "wlan0"

    # ── System WiFi route (SSH) ──────────────────────────────────────────────
    def ensure_system_wifi_route(self):
        """
        Đảm bảo default route hệ thống (main table) LUÔN trỏ vào wlan0.
        Đây là route dùng cho SSH và các ứng dụng hệ thống khác.
        KHÔNG BAO GIỜ xóa route này.
        """
        _, out, _ = run(["ip", "route", "show", "default", "dev", "wlan0"])
        if "default" in out:
            return  # Đã có, không cần làm gì

        gw = self.get_wifi_gateway()
        if not gw:
            log.warning("[SYS_ROUTE] Chưa tìm được WiFi gateway để restore system route")
            return

        rc, _, err = run_root(["ip", "route", "add", "default", "via", gw, "dev", "wlan0"])
        if rc == 0:
            log.info(f"[SYS_ROUTE] ✅ Restored system WiFi default route via {gw} (SSH OK)")
        else:
            log.warning(f"[SYS_ROUTE] Không restore được WiFi route: {err}")

    # ── PBR table 100 — chỉ ảnh hưởng DroneBridge ───────────────────────────
    def set_pbr_route(self, iface: str, gateway: str = None):
        """
        Cập nhật routing table 100 (dronebridge).
        CHỈ DroneBridge traffic (đã bị mark 0x01) mới đi theo route này.
        SSH và hệ thống KHÔNG bị ảnh hưởng.
        """
        wwan_ip = self.get_iface_ip("wwan0") if iface == "wwan0" else None

        if iface == "wwan0" and not os.path.exists("/sys/class/net/wwan0"):
            log.warning("[PBR] wwan0 chưa tồn tại — bỏ qua table 100, chờ dronebridge-4g-init")
            return False

        # QMI raw-IP (wwan0 point-to-point): dùng `default dev wwan0`, KHÔNG `via peer`.
        if iface == "wwan0":
            gateway = None
        elif iface != "wwan0":
            self.clear_pbr_wwan_snat()

        # Flush table cũ
        run_root(["ip", "route", "flush", "table", str(PBR_TABLE)])

        # PX4 LAN (eth0) — route cụ thể hơn default, tránh leak sang wwan0
        self.ensure_pbr_eth_lan_route()

        if gateway:
            cmd = ["ip", "route", "add", "default", "via", gateway,
                   "dev", iface, "table", str(PBR_TABLE)]
        else:
            cmd = ["ip", "route", "add", "default",
                   "dev", iface, "table", str(PBR_TABLE)]
            if wwan_ip:
                cmd.extend(["src", wwan_ip])

        rc, _, err = run_root(cmd)
        if rc == 0:
            gw_str = f"via {gateway}" if gateway else "dev-only"
            log.info(f"[PBR] table {PBR_TABLE}: {iface} ({gw_str}) → DroneBridge packets")
            if iface == "wwan0":
                self.ensure_pbr_wwan_host_routes(wwan_ip)
                self.ensure_pbr_wwan_snat(wwan_ip)
            return True
        else:
            log.error(f"[PBR] Không set được route table {PBR_TABLE}: {err}")
            return False

    def clear_pbr_wwan_snat(self):
        snat_ip = getattr(self, "_wwan_snat_ip", None)
        if not snat_ip:
            return
        run_root([
            "iptables", "-t", "nat", "-D", "POSTROUTING",
            "-o", "wwan0", "-m", "mark", "--mark", IPTABLES_MARK,
            "-j", "SNAT", "--to-source", snat_ip,
        ], timeout=5)
        self._wwan_snat_ip = None

    def ensure_pbr_wwan_snat(self, wwan_ip: str):
        """SNAT marked egress trên wwan0 — kernel hay chọn sai src (WiFi IP)."""
        if not wwan_ip:
            return
        if getattr(self, "_wwan_snat_ip", None) == wwan_ip:
            return
        self.clear_pbr_wwan_snat()
        rc, _, err = run_root([
            "iptables", "-t", "nat", "-A", "POSTROUTING",
            "-o", "wwan0", "-m", "mark", "--mark", IPTABLES_MARK,
            "-j", "SNAT", "--to-source", wwan_ip,
        ], timeout=5)
        if rc == 0:
            self._wwan_snat_ip = wwan_ip
            log.info(f"[PBR] SNAT wwan0 mark {FWMARK} → {wwan_ip}")
        else:
            log.error(f"[PBR] Không set SNAT wwan0: {err}")

    def _egress_hosts_for_wwan(self) -> list[str]:
        """Các host data-plane phải reachable qua table 100 khi 4G active."""
        hosts = set()
        if HEALTH_TCP_HOST:
            hosts.add(HEALTH_TCP_HOST)
        wg_ep = os.getenv("DRONEBRIDGE_WG_ENDPOINT", "45.117.171.237:51820").strip()
        if wg_ep:
            hosts.add(wg_ep.rsplit(":", 1)[0])
        return sorted(hosts)

    def ensure_pbr_wwan_host_routes(self, wwan_ip: str = None):
        """
        QMI raw-IP: table 100 cần /32 dev wwan0 cho auth + WG endpoint.
        """
        if not wwan_ip:
            wwan_ip = self.get_iface_ip("wwan0")
        for host in self._egress_hosts_for_wwan():
            cmd = [
                "ip", "route", "replace", f"{host}/32",
                "dev", "wwan0", "table", str(PBR_TABLE),
            ]
            if wwan_ip:
                cmd.extend(["src", wwan_ip])
            run_root(cmd)
        log.info(f"[PBR] table {PBR_TABLE}: host /32 routes → wwan0 ({', '.join(self._egress_hosts_for_wwan())})")

    def get_pbr_active_iface(self) -> str | None:
        """Đọc default route hiện tại trong table 100 để biết interface đang thực sự active."""
        rc, out, _ = run(["ip", "route", "show", "table", str(PBR_TABLE), "default"])
        if rc != 0 or not out.strip():
            return None
        m = re.search(r"\bdev\s+(\S+)", out)
        return m.group(1) if m else None

    def verify_pbr_rule(self):
        """Kiểm tra ip rule fwmark tồn tại, tự tạo lại nếu mất (sau reboot)."""
        _, out, _ = run(["ip", "rule", "show"])
        if re.search(r"fwmark\s+0x1(?:/0x1)?\s+lookup\s+" + re.escape(PBR_TABLE_NAME), out):
            return
        log.warning("[PBR] ip rule fwmark mất — tạo lại...")
        run_root(["ip", "rule", "add", "fwmark", FWMARK_MASK,
                  "table", str(PBR_TABLE), "priority", "100"])
        log.info(f"[PBR] ip rule restored: fwmark {FWMARK_MASK} → table {PBR_TABLE}")

    def verify_pbr_route(self, iface: str, gateway: str = None):
        """Đảm bảo table 100 luôn có default route đúng với interface active."""
        # Luôn giữ route PX4 LAN trong table 100 (kể cả khi default đã đúng)
        self.ensure_pbr_eth_lan_route()

        _, out, _ = run(["ip", "route", "show", "table", str(PBR_TABLE)])
        if not out.strip():
            log.warning(f"[PBR] table {PBR_TABLE} đang rỗng — khôi phục route cho {iface}")
            self.set_pbr_route(iface, gateway=gateway)
            return

        if iface in out and "default" in out:
            # wwan0 raw-IP: default qua `via peer` là sai — phải `default dev wwan0`
            if iface == "wwan0" and re.search(r"default\s+via\s+", out):
                log.warning(f"[PBR] table {PBR_TABLE} wwan0 dùng `via` — sửa lại dev-only")
                self.set_pbr_route(iface)
                return
            if iface == "wwan0":
                self.ensure_pbr_wwan_host_routes(self.get_iface_ip("wwan0"))
                self.ensure_pbr_wwan_snat(self.get_iface_ip("wwan0"))
            return

        log.warning(f"[PBR] table {PBR_TABLE} lệch interface active ({iface}) — sửa lại")
        self.set_pbr_route(iface, gateway=gateway)

    # ── 4G reinit via systemd ────────────────────────────────────────────────
    def _is_4g_init_busy(self) -> bool:
        """Tránh restart chồng khi 4g-init đang chạy (gây mất USB interface)."""
        # oneshot + RemainAfterExit=yes: sau khi xong vẫn ActiveState=active SubState=exited.
        # Chỉ coi là busy khi đang thực sự chạy ExecStart (activating / SubState=start).
        _, out, _ = run(
            [
                "systemctl", "show", SYSTEMD_4G_SERVICE,
                "-p", "ActiveState", "-p", "SubState", "--value",
            ],
            timeout=4,
        )
        values = [v.strip() for v in (out or "").splitlines() if v.strip()]
        active = values[0] if values else ""
        sub = values[1] if len(values) > 1 else ""
        return active == "activating" or sub == "start"

    def trigger_4g_reinit(self, wlan_ok=False) -> bool:
        """
        Yêu cầu systemd restart dronebridge-4g-init.service.

        NON-BLOCKING: Hàm này trả về ngay lập tức để không chặn vòng lặp
        routing. Kết quả phục hồi được phát hiện ở các lần gọi tiếp theo
        (mỗi MONITOR_INTERVAL_S giây).

        Returns:
            True  — 4G vừa phục hồi (wwan0 có IP) trong lần gọi này.
            False — chưa phục hồi hoặc đang trong thời gian backoff/chờ.
        """
        now = time.time()

        if self._is_4g_init_busy():
            log.info("[REINIT] Bỏ qua — dronebridge-4g-init đang chạy")
            return False

        # ── Đang chờ kết quả của một reinit trước đó ────────────────────
        if self._reinit_pending:
            elapsed = now - self._reinit_pending_t
            wwan_ip = self.get_iface_ip("wwan0")
            if wwan_ip:
                log.info(f"✅ [REINIT] 4G phục hồi sau {elapsed:.0f}s (IP: {wwan_ip})")
                self._reinit_pending   = False
                self._reinit_count     = 0  # reset backoff khi thành công
                return True

            if elapsed >= REINIT_TIMEOUT_S:
                log.error(
                    f"❌ [REINIT] 4G không phục hồi sau {elapsed:.0f}s "
                    f"(timeout {REINIT_TIMEOUT_S}s)"
                )
                self._reinit_pending = False
                # _reinit_count đã được tăng khi trigger, giữ nguyên để backoff
            else:
                log.debug(
                    f"[REINIT] ⏳ Đang chờ 4G: {elapsed:.0f}s / {REINIT_TIMEOUT_S}s"
                )
            return False

        # ── Kiểm tra Exponential Backoff ────────────────────────────────
        if wlan_ok:
            # Khi fallback WiFi (drone đang bay), retry 4G nhanh hơn để kéo lại sớm.
            base_wait_min = REINIT_RETRY_MIN_WIFI * (2 ** self._reinit_count)
            base_wait_min = min(base_wait_min, max(1, REINIT_RETRY_MAX_WIFI))
        else:
            base_wait_min = REINIT_RETRY_MIN * (2 ** self._reinit_count)
            if base_wait_min > 60:
                base_wait_min = 60

        elapsed_min = (now - self._last_reinit) / 60
        if self._last_reinit > 0 and elapsed_min < base_wait_min:
            if int(elapsed_min * 60) % 60 < MONITOR_INTERVAL_S:
                log.info(
                    f"[REINIT] Throttle: {elapsed_min:.1f}/{base_wait_min} min "
                    f"(Lvl {self._reinit_count}) — đang bảo vệ hardware"
                )
            return False

        # ── Trigger reinit ───────────────────────────────────────────────
        log.warning(
            f"⚡ [REINIT] Triggering {SYSTEMD_4G_SERVICE} restart "
            f"(Attempt #{self._reinit_count + 1})..."
        )
        self._last_reinit      = now
        self._reinit_count    += 1
        self._reinit_pending   = True
        self._reinit_pending_t = now

        # timeout=15s chỉ áp dụng cho subprocess systemctl (client), không phải
        # thời gian chạy thực của service — systemd tiếp tục chạy service nền.
        run_root(["systemctl", "restart", SYSTEMD_4G_SERVICE], timeout=15)
        log.info(
            f"[REINIT] dronebridge-4g-init.service đã được kích hoạt — "
            f"polling phục hồi mỗi {MONITOR_INTERVAL_S}s (tối đa {REINIT_TIMEOUT_S}s)"
        )
        return False  # chưa phục hồi ngay, sẽ detect ở lần gọi tiếp theo
    def apply_routing_policy(self):
        """
        Chọn interface PBR table 100 — SYSTEM_ARCHITECTURE.md §5.
        Go app đọc network_status.json; monitorIPChange() tự ForceReconnect khi egress đổi.
        """
        network_mode = load_network_mode()
        force_4g_only = force_4g_only_from_policy()

        # Đồng bộ eth + wlan reply trước; main default sau khi biết 4G probe
        wlan_admin, eth_plane = self.ensure_local_planes()

        # Đảm bảo ip rule còn tồn tại (có thể mất sau reboot nếu không persist)
        self.verify_pbr_rule()

        # active interface phản ánh route thực sự trong table 100
        self._active_iface = self.get_pbr_active_iface()

        wwan_ip = self.get_iface_ip("wwan0")
        wlan_ip = self.get_iface_ip("wlan0")

        if network_mode == "wifi_only":
            gw = self.get_wifi_gateway()
            if wlan_ip:
                if self._active_iface != "wlan0":
                    self.set_pbr_route("wlan0", gateway=gw)
                self.verify_pbr_route("wlan0", gateway=gw)
            self._active_iface = self.get_pbr_active_iface()
            self._wwan_probe_mode = "WIFI_ONLY"
            wlan_ok = bool(wlan_ip) and self.ping_via("wlan0", count=1)
            self._main_default_iface = self.ensure_system_default_route(False, network_mode)
            self.log_link_event(wwan_ip, wlan_ip, False, wlan_ok)
            self.log_udp_wwan0_counter()
            self._save_status(
                wwan_ip, wlan_ip, False, wlan_ok, network_mode, "WIFI_ONLY",
                wlan_admin=wlan_admin, eth_plane=eth_plane,
            )
            return

        # 4G usable check:
        # 1) ưu tiên ICMP ping trực tiếp qua wwan0
        # 2) fallback TCP probe (auth host) khi carrier chặn ICMP
        wwan_ping_ok = bool(wwan_ip) and self.ping_via("wwan0", count=1)
        wwan_tcp_ok = False
        if bool(wwan_ip) and not wwan_ping_ok:
            wwan_tcp_ok = self.tcp_probe_via_wwan(wwan_ip)
        wwan_ok = wwan_ping_ok or wwan_tcp_ok
        if wwan_ping_ok:
            self._wwan_probe_mode = "PING_OK"
        elif wwan_tcp_ok:
            self._wwan_probe_mode = "TCP_OK"
        else:
            self._wwan_probe_mode = "FAIL"

        wlan_ok = (
            bool(wlan_ip)
            and not wlan_radio_blocked()
            and self.ping_via("wlan0", count=1)
        )

        target_iface, route_reason = self._resolve_pbr_target(
            network_mode, wwan_ok, wlan_ok, force_4g_only,
        )

        if target_iface == "wwan0":
            gw = self.get_wwan_gateway()
            if wwan_ok:
                self._4g_failures = 0
                self._reinit_count = 0
                if self._active_iface != "wwan0":
                    log.info(
                        f"✅ DroneBridge → 4G ({wwan_ip})"
                        + (f" | SSH → WiFi ({wlan_ip}) [unchanged]" if wlan_ok else "")
                    )
            elif route_reason == "4G_WAITING":
                if self._4g_failures == 0 or self._4g_failures % 10 == 0:
                    elapsed = int(time.time() - self._4g_down_since) if self._4g_down_since else 0
                    log.info(
                        f"⏳ 4G down — giữ wwan0, chờ phục hồi "
                        f"({elapsed}s / {load_fallback_delay()}s trước WiFi fallback)"
                    )
            elif force_4g_only:
                if self._4g_failures == FAIL_THRESHOLD or self._4g_failures % 30 == 0:
                    log.warning(
                        f"⚠️  4G down (failure #{self._4g_failures}) — 4G-ONLY: giữ wwan0"
                    )
            if self._active_iface != "wwan0":
                self.set_pbr_route("wwan0", gateway=gw)
            self.verify_pbr_route("wwan0", gateway=gw)
        elif target_iface == "wlan0":
            gw = self.get_wifi_gateway()
            if self._active_iface != "wlan0":
                self.set_pbr_route("wlan0", gateway=gw)
                log.warning(
                    f"⚠️  DroneBridge fallback → WiFi ({wlan_ip}) "
                    f"(4G down > {load_fallback_delay()}s)"
                )
            self.verify_pbr_route("wlan0", gateway=gw)

        self._active_iface = self.get_pbr_active_iface()

        if not wwan_ok and network_mode != "wifi_only":
            self._4g_failures += 1
            if self._4g_failures >= FAIL_THRESHOLD:
                wlan_for_reinit = wlan_ok and target_iface == "wlan0"
                if self.trigger_4g_reinit(wlan_ok=wlan_for_reinit):
                    self._4g_failures = 0
        elif route_reason == "4G_DOWN_NO_WIFI":
            if self._4g_failures == FAIL_THRESHOLD or self._4g_failures % 10 == 0:
                log.error(f"❌ Không có kết nối nào cho DroneBridge! fail #{self._4g_failures}")

        self._main_default_iface = self.ensure_system_default_route(wwan_ok, network_mode)

        self.log_link_event(wwan_ip, wlan_ip, wwan_ok, wlan_ok)
        self.log_udp_wwan0_counter()
        self._save_status(
            wwan_ip, wlan_ip, wwan_ok, wlan_ok, network_mode, route_reason,
            wlan_admin=wlan_admin, eth_plane=eth_plane,
        )

    def _build_4g_status(self, wwan_ip, wwan_ok: bool) -> dict:
        block = {"ip": wwan_ip, "online": wwan_ok}
        if not wwan_ip:
            return block
        sig = self.get_4g_signal_metrics()
        block["signal"] = sig.get("rssi", "N/A")
        block["signal_quality"] = sig.get("quality", "UNKNOWN")
        block["rsrp"] = sig.get("rsrp", "N/A")
        if sig.get("signal_dbm") is not None:
            block["signal_dbm"] = sig["signal_dbm"]
        return block

    def _build_wifi_status(self, wlan_ip, wlan_ok: bool) -> dict:
        block = {"ip": wlan_ip, "online": wlan_ok}
        if wlan_ip:
            dbm = self.get_wifi_signal_dbm()
            if dbm is not None:
                block["signal_dbm"] = dbm
        return block

    def _build_cloud_uplink_status(
        self,
        active_iface: str | None,
        wwan_ip,
        wlan_ip,
        wwan_ok: bool,
        wlan_ok: bool,
        g4_block: dict,
        wifi_block: dict,
    ) -> dict:
        """
        Trạng thái cloud data plane (table 100) — contract cho Go/LCD/Web.
        ready = PBR default khớp active_iface + có IP + probe OK.
        """
        cloud = {
            "interface": active_iface,
            "table": PBR_TABLE,
            "ip": None,
            "online": False,
            "probe": "NONE",
            "ready": False,
            "signal_dbm": None,
            "signal_source": None,
        }
        if not active_iface:
            return cloud

        pbr_iface = self.get_pbr_active_iface()
        if active_iface == "wwan0":
            cloud["ip"] = wwan_ip
            cloud["online"] = bool(wwan_ok)
            cloud["probe"] = self._wwan_probe_mode
            if g4_block.get("signal_dbm") is not None:
                cloud["signal_dbm"] = g4_block["signal_dbm"]
                cloud["signal_source"] = "4g"
        elif active_iface == "wlan0":
            cloud["ip"] = wlan_ip
            cloud["online"] = bool(wlan_ok)
            cloud["probe"] = "PING_OK" if wlan_ok else "FAIL"
            if wifi_block.get("signal_dbm") is not None:
                cloud["signal_dbm"] = wifi_block["signal_dbm"]
                cloud["signal_source"] = "wifi"

        cloud["ready"] = (
            pbr_iface == active_iface
            and bool(cloud["ip"])
            and bool(cloud["online"])
        )
        return cloud

    def _save_status(
        self, wwan_ip, wlan_ip, wwan_ok, wlan_ok,
        network_mode="prefer_4g", route_reason="",
        wlan_admin=None, eth_plane=None,
    ):
        """Ghi network_status.json — single source of truth cho routing + UI."""
        g4_block = self._build_4g_status(wwan_ip, wwan_ok)
        wifi_block = self._build_wifi_status(wlan_ip, wlan_ok)
        cloud_uplink = self._build_cloud_uplink_status(
            self._active_iface, wwan_ip, wlan_ip, wwan_ok, wlan_ok,
            g4_block, wifi_block,
        )
        status = {
            "timestamp":        int(time.time()),
            "active_interface": self._active_iface,
            "route_reason":     route_reason,
            "network_mode":     network_mode,
            "wwan_probe_mode":  self._wwan_probe_mode,
            "pbr_table":        PBR_TABLE,
            "cloud_ready":      cloud_uplink["ready"],
            "cloud_signal_dbm": cloud_uplink["signal_dbm"],
            "cloud_signal_source": cloud_uplink["signal_source"],
            "planes": {
                "eth_pixhawk": eth_plane or {},
                "wlan_admin":  wlan_admin or {},
                "cloud_uplink": cloud_uplink,
            },
            "4g":  g4_block,
            "wifi": wifi_block,
            "main_default": self._main_default_iface,
            "ssh_route": (
                f"wlan0 (admin reply table {WLAN_REPLY_TABLE})"
                if self._main_default_iface == "wwan0"
                else "wlan0 (system default)"
            ),
        }
        try:
            with open(STATUS_FILE, "w") as f:
                json.dump(status, f, indent=2)
            data_copy = os.path.join(
                os.getenv("DRONEBRIDGE_DATA_DIR", "/opt/dronebridge/data"),
                "network_status.json",
            )
            os.makedirs(os.path.dirname(data_copy), exist_ok=True)
            with open(data_copy, "w") as f:
                json.dump(status, f, indent=2)
        except Exception as e:
            log.warning(f"Không ghi được status file: {e}")

    # ── Main loop ────────────────────────────────────────────────────────────
    def run(self):
        log.info("=" * 60)
        log.info(" DroneBridge Network Monitor v2.0 — PBR Mode")
        log.info(f" PBR table: {PBR_TABLE} ({PBR_TABLE_NAME})")
        log.info(f" Mark: {FWMARK} | Interval: {MONITOR_INTERVAL_S}s")
        log.info(f" Config network.mode: {load_network_mode()}")
        log.info(f" PBR policy: {'4g-only-hold' if force_4g_only_from_policy() else 'prefer-4g-with-wifi-fallback'}")
        log.info(f" Fail threshold: {FAIL_THRESHOLD} | Reinit timeout: {REINIT_TIMEOUT_S}s")
        log.info(f" Fallback delay: {load_fallback_delay()}s (prefer_4g WiFi fallback)")
        log.info(f" Cloud WiFi fallback: {load_cloud_wifi_fallback()}")
        log.info("=" * 60)

        while True:
            try:
                self.apply_routing_policy()
            except Exception as e:
                log.exception(f"Unexpected error in routing policy: {e}")
            time.sleep(MONITOR_INTERVAL_S)


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "status":
        # Quick status check
        m = NetworkMonitor()
        wwan = m.get_iface_ip("wwan0")
        wlan = m.get_iface_ip("wlan0")
        wwan_ok = bool(wwan) and m.ping_via("wwan0")
        wlan_ok = bool(wlan) and m.ping_via("wlan0")
        _, pbr_routes, _ = run(["ip", "route", "show", "table", str(PBR_TABLE)])
        _, sys_route, _ = run(["ip", "route", "show", "default"])
        print(f"4G:   {'✅' if wwan_ok else '❌'} {wwan or 'no IP'}")
        print(f"WiFi: {'✅' if wlan_ok else '❌'} {wlan or 'no IP'}")
        print(f"PBR table {PBR_TABLE}: {pbr_routes or 'empty'}")
        print(f"System default: {sys_route or 'none'}")
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "once":
        # Áp dụng routing một lần rồi thoát (dùng sau 4g-init, không chạy loop)
        NetworkMonitor().apply_routing_policy()
        return

    NetworkMonitor().run()


if __name__ == "__main__":
    main()
