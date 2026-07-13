import os
import socket
import threading
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from pymavlink import mavutil

from mavlink_utils import (
    MAVLINK_PATH_ETHERNET,
    MAVLINK_PATH_SERIAL,
    is_pixhawk_heartbeat,
    normalize_connection_type,
)
from mavlink_custom import (
    COMP_ONBOARD,
    COMPANION_SYS_ID,
    GPS_DIAG_NO_PX4_STREAM,
    GPS_DIAG_PX4_LOCAL_ONLY,
    GPS_DIAG_PX4_NO_FIX,
    GPS_DIAG_PX4_OK,
    build_dronebridge_status_frame,
    build_session_heartbeat_frame,
    build_session_heartbeat_frame_shifted,
    forward_gps_raw_int,
    session_hb_mode,
)
from metrics import global_metrics
from network_utils import detect_network_info, get_local_ip
from telemetry import global_telemetry
from web.mavlink_bridge import bridge

logger = logging.getLogger("Forwarder")


class Forwarder:
    def __init__(self, config, auth_client, vpn_manager=None):
        self.config = config
        self.auth_client = auth_client
        self.vpn_manager = vpn_manager
        self.network = config.network
        self.ethernet = config.ethernet or {}
        self.running = False
        self.server_sock = None
        self.target_addr = (
            config.forwarding.get("target_host"),
            config.forwarding.get("target_port"),
        )
        self.tcp_host = self.network.get("tcp_host", "0.0.0.0")
        self.tcp_port = self.network.get("local_listen_port", self.network.get("tcp_port", 14540))
        self.connection_type = normalize_connection_type(self.network.get("connection_type", "serial"))

        self._connections: Dict[str, object] = {}
        self._path_lock = threading.RLock()
        self._active_path = ""
        self._active_conn = None
        self._eth_heartbeat_at: Optional[datetime] = None
        self._serial_heartbeat_at: Optional[datetime] = None

        self._pixhawk_connected = threading.Event()
        self._pixhawk_sys_id = 0
        self._is_healthy = True
        self._previous_ip = ""

        self.stats_lock = threading.Lock()
        self.stats = {
            "rawIn": 0,
            "accepted": 0,
            "outServer": 0,
            "dropErr": 0,
            "dropNoPixhawk": 0,
            "dropUnhealthy": 0,
            "dropVpnNotReady": 0,
        }
        self._rate_lock = threading.Lock()
        self._rate_raw_in = 0
        self._rate_accepted = 0
        self._rate_out_server = 0
        self._rate_bytes_out = 0

        self._gps_last_at: Optional[datetime] = None
        self._gps_fix_type = 0
        self._gps_satellites = 0
        self._local_pos_last_at: Optional[datetime] = None
        self._hb_seq = 0
        self._companion_seq = 0
        self._mavlink_ka_seq = 0

    def _fallback_timeout_sec(self) -> float:
        timeout = self.ethernet.get("pixhawk_connection_timeout", 30)
        sec = max(3, int(timeout) // 2)
        return float(sec)

    def _listen_port(self) -> int:
        return int(self.network.get("local_listen_port") or self.network.get("tcp_port") or 14550)

    def _ethernet_udpin_spec(self) -> str:
        """Bind fixed UDP on ethernet.local_ip (PX4 sends unicast to CM5 IP:port)."""
        port = self._listen_port()
        local_ip = str(self.ethernet.get("local_ip") or "").strip()
        if local_ip:
            return f"udpin:{local_ip}:{port}"
        return f"udpin:0.0.0.0:{port}"

    def _pixhawk_udp_target(self) -> Optional[Tuple[str, int]]:
        pixhawk_ip = str(self.ethernet.get("pixhawk_ip") or "").strip()
        if not pixhawk_ip:
            return None
        port = int(self.ethernet.get("pixhawk_port") or 0)
        if port <= 0:
            port = self._listen_port()
        return pixhawk_ip, port

    def _create_connection(self, path: str):
        if path == MAVLINK_PATH_SERIAL:
            device = self.network.get("serial_port", "/dev/ttyAMA2")
            baud = int(self.network.get("serial_baud", 57600))
            if not os.path.exists(device):
                raise FileNotFoundError(f"serial device {device} not available")
            logger.info("[MAVLINK] Serial listener enabled on %s @ %d baud", device, baud)
            return mavutil.mavlink_connection(device, baud=baud)

        if self.connection_type == "tcp_listen":
            logger.info("[MAVLINK] Listening for Pixhawk via TCP port %s", self.tcp_port)
            return mavutil.mavlink_connection(f"tcpin:{self.tcp_host}:{self.tcp_port}")
        if self.connection_type == "tcp_client":
            logger.info("[MAVLINK] Connecting to Pixhawk via TCP: %s:%s", self.tcp_host, self.tcp_port)
            return mavutil.mavlink_connection(f"tcp:{self.tcp_host}:{self.tcp_port}")

        spec = self._ethernet_udpin_spec()
        logger.info("[MAVLINK] Pixhawk UDP listener %s (fixed port for ETH partner)", spec)
        return mavutil.mavlink_connection(spec)

    def start_listener(self) -> bool:
        paths = []
        if self.connection_type == MAVLINK_PATH_SERIAL:
            paths = [MAVLINK_PATH_SERIAL]
        elif self.connection_type == "prefer_ethernet":
            paths = [MAVLINK_PATH_ETHERNET, MAVLINK_PATH_SERIAL]
        else:
            paths = [MAVLINK_PATH_ETHERNET]

        for path in paths:
            try:
                self._connections[path] = self._create_connection(path)
            except Exception as exc:
                if path == MAVLINK_PATH_SERIAL and self.connection_type == "prefer_ethernet":
                    logger.warning("[MAVLINK] Serial backup disabled: %s", exc)
                    continue
                logger.error("[MAVLINK] Failed to open %s listener: %s", path, exc)
                return False

        if not self._connections:
            return False

        self._refresh_active_path(datetime.now(timezone.utc))
        return True

    def _vpn_ready(self) -> bool:
        if not self.vpn_manager or not self.vpn_manager.is_enabled():
            return True
        return self.vpn_manager.is_running() and bool(self.vpn_manager.get_assigned_ip())

    def _create_server_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        vpn_ip = self.vpn_manager.get_assigned_ip() if self.vpn_manager else ""
        if vpn_ip:
            sock.bind((vpn_ip, 0))
            logger.info("[FORWARDER] MAVLink uplink via VPN %s → %s", vpn_ip, self.target_addr)
        elif self.vpn_manager and self.vpn_manager.is_enabled():
            logger.warning(
                "[FORWARDER] VPN chưa sẵn sàng — gói tới %s sẽ không tới server",
                self.target_addr,
            )
        return sock

    def start(self) -> bool:
        if not self.start_listener():
            return False

        self.server_sock = self._create_server_socket()
        self.running = True

        for path, conn in self._connections.items():
            threading.Thread(
                target=self._uplink_loop,
                args=(path, conn),
                daemon=True,
                name=f"forwarder-uplink-{path}",
            ).start()

        threading.Thread(target=self._downlink_loop, daemon=True, name="forwarder-downlink").start()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="forwarder-heartbeat").start()
        auth_cfg = getattr(self.config, "auth", {}) or {}
        if float(auth_cfg.get("session_heartbeat_frequency", 1.0) or 0) > 0:
            threading.Thread(
                target=self._mavlink_keepalive_loop,
                daemon=True,
                name="forwarder-mavlink-ka",
            ).start()
        threading.Thread(
            target=self._companion_status_loop,
            daemon=True,
            name="forwarder-companion-status",
        ).start()
        if not forward_gps_raw_int(self.network):
            logger.info("[MAVLINK] GPS_RAW_INT uplink OFF — server uses EKF/global position only")
        self._start_partner_heartbeat()
        threading.Thread(target=self._path_watchdog_loop, daemon=True, name="forwarder-path-watchdog").start()
        threading.Thread(target=self._ip_monitor_loop, daemon=True, name="forwarder-ip-monitor").start()
        threading.Thread(target=self._rate_reporter_loop, daemon=True, name="forwarder-rate-reporter").start()

        logger.info("Forwarder started. Target: %s, mode=%s", self.target_addr, self.connection_type)
        global_metrics.add_log("INFO", f"Forwarder started -> {self.target_addr}")
        return True

    def _note_heartbeat_path(self, path: str) -> str:
        now = datetime.now(timezone.utc)
        with self._path_lock:
            if path == MAVLINK_PATH_ETHERNET:
                self._eth_heartbeat_at = now
            elif path == MAVLINK_PATH_SERIAL:
                self._serial_heartbeat_at = now
            self._refresh_active_path(now)
            return self._active_path

    def _refresh_active_path(self, now: datetime) -> None:
        timeout = self._fallback_timeout_sec()
        eth_fresh = self._eth_heartbeat_at and (now - self._eth_heartbeat_at).total_seconds() <= timeout
        serial_fresh = self._serial_heartbeat_at and (now - self._serial_heartbeat_at).total_seconds() <= timeout

        preferred = self.connection_type
        new_path = self._active_path

        if preferred == MAVLINK_PATH_SERIAL:
            if serial_fresh:
                new_path = MAVLINK_PATH_SERIAL
        elif preferred == "prefer_ethernet":
            if eth_fresh:
                new_path = MAVLINK_PATH_ETHERNET
            elif serial_fresh:
                new_path = MAVLINK_PATH_SERIAL
        else:
            if eth_fresh:
                new_path = MAVLINK_PATH_ETHERNET
            elif serial_fresh:
                new_path = MAVLINK_PATH_SERIAL

        if not new_path and self._connections:
            new_path = next(iter(self._connections.keys()))

        changed = new_path and new_path != self._active_path
        if new_path:
            self._active_path = new_path
            self._active_conn = self._connections.get(new_path)

        eth_ok = self._eth_heartbeat_at is not None
        serial_ok = self._serial_heartbeat_at is not None
        bridge.set_mavlink_path(self._active_path, eth_ok, serial_ok)
        if self._active_conn is not None:
            bridge.set_connection(self._active_conn)
        if changed:
            logger.info("[MAVLINK] Active PX4 path switched to %s", self._active_path)
            global_metrics.add_log("INFO", f"Active PX4 path switched to {self._active_path}")

    def _path_watchdog_loop(self) -> None:
        while self.running:
            with self._path_lock:
                self._refresh_active_path(datetime.now(timezone.utc))
            time.sleep(1)

    def _note_raw_in(self, msg) -> None:
        with self._rate_lock:
            self._rate_raw_in += 1

    def _note_out_server(self, buf: bytes) -> None:
        with self._rate_lock:
            self._rate_out_server += 1
            self._rate_bytes_out += len(buf)

    def _rate_reporter_loop(self) -> None:
        while self.running:
            time.sleep(1)
            with self._rate_lock:
                raw = self._rate_raw_in
                accepted = self._rate_accepted
                out = self._rate_out_server
                bytes_out = self._rate_bytes_out
                self._rate_raw_in = 0
                self._rate_accepted = 0
                self._rate_out_server = 0
                self._rate_bytes_out = 0
            if bytes_out == 0 and out > 0:
                bytes_out = int(out * 120)
            global_metrics.set_udp_rates(raw, accepted, out, bytes_out)

    def _process_uplink_message(self, msg, path: str) -> None:
        msg_type = msg.get_type()
        sys_id = msg.get_srcSystem()

        if msg_type in ("HEARTBEAT", "VFR_HUD", "GLOBAL_POSITION_INT", "GPS_RAW_INT", "SYS_STATUS"):
            global_telemetry.feed(msg)

        if msg_type == "GPS_RAW_INT":
            self._gps_last_at = datetime.now(timezone.utc)
            self._gps_fix_type = int(getattr(msg, "fix_type", 0) or 0)
            self._gps_satellites = int(getattr(msg, "satellites_visible", 0) or 0)
        elif msg_type == "LOCAL_POSITION_NED":
            self._local_pos_last_at = datetime.now(timezone.utc)

        if msg_type == "HEARTBEAT":
            if not is_pixhawk_heartbeat(msg):
                logger.debug(
                    "[REJECT] Non-Pixhawk heartbeat (SysID: %s, Type: %s, Autopilot: %s)",
                    sys_id,
                    getattr(msg, "type", None),
                    getattr(msg, "autopilot", None),
                )
                return

            active_path = self._note_heartbeat_path(path)
            if not self._pixhawk_connected.is_set():
                self._pixhawk_connected.set()
                self._pixhawk_sys_id = sys_id
                logger.info(
                    "[PIXHAWK_CONNECTED] First heartbeat from Pixhawk (SysID: %s, path: %s)",
                    sys_id,
                    active_path,
                )
                global_metrics.add_log("INFO", f"Pixhawk connected via {active_path}")
            bridge.handle_heartbeat(sys_id, active_path)
            return

        if msg_type == "PARAM_VALUE":
            bridge.handle_param_value(msg)

        if not self._pixhawk_connected.is_set():
            with self.stats_lock:
                self.stats["dropNoPixhawk"] += 1
            return

        with self.stats_lock:
            self.stats["rawIn"] += 1
        with self._rate_lock:
            self._rate_accepted += 1

        if not self._is_healthy:
            with self.stats_lock:
                self.stats["dropUnhealthy"] += 1
            global_metrics.inc_failed_unhealthy(msg_type)
            return

        if not self._vpn_ready():
            with self.stats_lock:
                self.stats["dropVpnNotReady"] += 1
            return

        if not self.auth_client.session_token:
            with self.stats_lock:
                self.stats["dropErr"] += 1
            global_metrics.inc_failed_unhealthy(msg_type)
            return

        if msg_type == "GPS_RAW_INT" and not forward_gps_raw_int(self.network):
            return

        try:
            buf = msg.get_msgbuf()
            self.server_sock.sendto(buf, self.target_addr)
            with self.stats_lock:
                self.stats["accepted"] += 1
                self.stats["outServer"] += 1
            self._note_out_server(buf)
            global_metrics.inc_sent(msg_type)
            global_metrics.inc_sent("outServer")
        except OSError as exc:
            with self.stats_lock:
                self.stats["dropErr"] += 1
            global_metrics.inc_failed_send(msg_type)
            global_metrics.add_log("ERROR", f"Forward send failed: {exc}")

    def _uplink_loop(self, path: str, conn) -> None:
        while self.running:
            try:
                msg = conn.recv_match(blocking=True, timeout=1.0)
                if msg is None:
                    continue
                self._note_raw_in(msg)
                self._process_uplink_message(msg, path)
            except Exception as exc:
                logger.error("Uplink error on %s: %s", path, exc)
                global_metrics.add_log("ERROR", f"Uplink error on {path}: {exc}")
                time.sleep(1)

    def _downlink_loop(self) -> None:
        while self.running:
            try:
                data, addr = self.server_sock.recvfrom(4096)
                if addr != self.target_addr:
                    continue
                conn = self._active_conn
                if conn is None:
                    continue
                conn.write(data)
            except Exception:
                pass
            time.sleep(0.01)

    def _start_partner_heartbeat(self) -> None:
        target = self._pixhawk_udp_target()
        conn = self._connections.get(MAVLINK_PATH_ETHERNET)
        if not target or conn is None or not hasattr(conn, "port"):
            if target and conn is None:
                logger.warning("[PARTNER_HB] No ethernet MAVLink listener — partner heartbeat skipped")
            return
        threading.Thread(
            target=self._partner_heartbeat_loop,
            args=(conn.port, target[0], target[1]),
            daemon=True,
            name="partner-heartbeat",
        ).start()

    def _partner_heartbeat_loop(self, sock: socket.socket, pixhawk_ip: str, pixhawk_port: int) -> None:
        """Share pymavlink UDP socket — PX4 unicast goes to same bind as listener (Pi pixhawk_udp.go)."""
        from pymavlink import mavutil as pm

        mav = pm.mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
        target = (pixhawk_ip, pixhawk_port)
        sent = 0
        first = False
        last_log = time.time()
        logger.info("[PARTNER_HB] HEARTBEAT 1 Hz via %s → %s:%d", sock.getsockname(), pixhawk_ip, pixhawk_port)
        while self.running:
            try:
                msg = mav.heartbeat_encode(
                    type=pm.mavlink.MAV_TYPE_GCS,
                    autopilot=pm.mavlink.MAV_AUTOPILOT_INVALID,
                    base_mode=0,
                    custom_mode=0,
                    system_status=pm.mavlink.MAV_STATE_ACTIVE,
                )
                sock.sendto(msg.pack(mav), target)
                sent += 1
                if not first:
                    logger.info("[PARTNER_HB] ✓ First HEARTBEAT sent → %s:%d", pixhawk_ip, pixhawk_port)
                    first = True
                elif time.time() - last_log >= 60:
                    logger.info("[PARTNER_HB] active → %s:%d (sent %d)", pixhawk_ip, pixhawk_port, sent)
                    last_log = time.time()
            except OSError as exc:
                logger.error("[PARTNER_HB] send failed: %s", exc)
            time.sleep(1)

    def _gps_diagnosis(self) -> tuple:
        now = datetime.now(timezone.utc)
        stale_after = 5.0
        if self._gps_last_at and (now - self._gps_last_at).total_seconds() <= stale_after:
            if self._gps_fix_type >= 3 and self._gps_satellites > 0:
                return self._gps_fix_type, self._gps_satellites, 1, GPS_DIAG_PX4_OK
            return self._gps_fix_type, self._gps_satellites, 1, GPS_DIAG_PX4_NO_FIX
        if self._local_pos_last_at and (now - self._local_pos_last_at).total_seconds() <= stale_after:
            return 255, 0, 0, GPS_DIAG_PX4_LOCAL_ONLY
        return 255, 0, 0, GPS_DIAG_NO_PX4_STREAM

    def _camera_live_flags(self) -> tuple:
        try:
            from web.camera_service import read_stream_stats

            cam0 = 1 if read_stream_stats(0, 5.0) else 0
            cam1 = 1 if read_stream_stats(1, 5.0) else 0
            return cam0, cam1
        except Exception:
            return 0, 0

    def _mavlink_keepalive_loop(self) -> None:
        auth_cfg = getattr(self.config, "auth", {}) or {}
        interval = float(auth_cfg.get("session_heartbeat_frequency", 1.0) or 1.0)
        if interval <= 0:
            return
        hb_mode = session_hb_mode()
        sequence = 0
        sys_id = self._pixhawk_sys_id or 1
        while self.running:
            token = self.auth_client.session_token
            expires_at = int(getattr(self.auth_client, "expires_at", 0) or 0)
            pixhawk_active = 1 if self._pixhawk_connected.is_set() else 0
            if token and self.server_sock:
                try:
                    if hb_mode == "shifted" and len(token) == 64:
                        frame = build_session_heartbeat_frame_shifted(
                            sys_id,
                            COMP_ONBOARD,
                            self._mavlink_ka_seq,
                            token,
                            expires_at,
                            sequence,
                            pixhawk_active,
                        )
                    else:
                        frame = build_session_heartbeat_frame(
                            sys_id,
                            COMP_ONBOARD,
                            self._mavlink_ka_seq,
                            token,
                            expires_at,
                            sequence,
                            pixhawk_active,
                        )
                    self.server_sock.sendto(frame, self.target_addr)
                    self._mavlink_ka_seq = (self._mavlink_ka_seq + 1) & 0xFF
                    sequence = (sequence + 1) & 0xFFFF
                except OSError as exc:
                    global_metrics.add_log("WARN", f"MAVLink session keepalive failed: {exc}")
            time.sleep(interval)

    def _companion_status_loop(self) -> None:
        while self.running:
            if self.server_sock and self.auth_client.session_token:
                fix, sats, px4_stream, diag = self._gps_diagnosis()
                cam0, cam1 = self._camera_live_flags()
                try:
                    frame = build_dronebridge_status_frame(
                        COMPANION_SYS_ID,
                        COMP_ONBOARD,
                        self._companion_seq,
                        timestamp_ms=int(time.time() * 1000) & 0xFFFFFFFF,
                        gps_fix_type=fix,
                        gps_satellites=sats,
                        gps_px4_streaming=px4_stream,
                        gps_diagnosis=diag,
                        camera0_live=cam0,
                        camera1_live=cam1,
                    )
                    self.server_sock.sendto(frame, self.target_addr)
                    self._companion_seq = (self._companion_seq + 1) & 0xFF
                except OSError:
                    pass
            time.sleep(1)

    def _heartbeat_loop(self) -> None:
        while self.running:
            packet = self.auth_client.get_session_refresh_packet()
            if packet:
                try:
                    self.server_sock.sendto(packet, self.target_addr)
                except OSError as exc:
                    global_metrics.inc_failed_send("session_refresh")
                    global_metrics.add_log("WARN", f"Session refresh send failed: {exc}")
            time.sleep(1)

    def _ip_monitor_loop(self) -> None:
        network_was_down = False
        while self.running:
            current_ip = get_local_ip()
            network_type, network_speed = detect_network_info()
            global_metrics.set_network_info(network_type, network_speed)

            if not current_ip:
                if not network_was_down:
                    logger.warning("[IP_MONITOR] Network lost (no valid IP)")
                    global_metrics.add_log("WARN", "Network lost - no valid IP")
                    self._is_healthy = False
                    network_was_down = True
            else:
                if network_was_down:
                    logger.info("[IP_MONITOR] Network restored: IP=%s", current_ip)
                    global_metrics.add_log("INFO", f"Network restored: IP={current_ip}")
                    global_metrics.set_ip(current_ip)
                    self._previous_ip = current_ip
                    self._is_healthy = True
                    network_was_down = False
                    if self.auth_client:
                        self.auth_client.force_reconnect()
                elif not self._previous_ip:
                    self._previous_ip = current_ip
                    global_metrics.set_ip(current_ip)
                    global_metrics.add_log("INFO", f"Initial IP: {current_ip}")
                    self._is_healthy = True
                elif self._previous_ip != current_ip:
                    logger.warning("[IP_MONITOR] IP changed: %s -> %s", self._previous_ip, current_ip)
                    global_metrics.add_log("WARN", f"IP changed: {self._previous_ip} -> {current_ip}")
                    global_metrics.set_ip(current_ip)
                    self._previous_ip = current_ip
                    self._is_healthy = True
                    if self.auth_client:
                        self.auth_client.force_reconnect()
                else:
                    global_metrics.set_ip(current_ip)
                    self._is_healthy = True

            time.sleep(5)

    def get_active_connection(self):
        return self._active_conn

    def rebind_vpn_socket(self) -> None:
        if not self.running:
            return
        try:
            new_sock = self._create_server_socket()
            old = self.server_sock
            self.server_sock = new_sock
            if old:
                old.close()
            logger.info("[FORWARDER] UDP sender rebound after VPN up")
        except Exception as exc:
            logger.error("[FORWARDER] VPN socket rebind failed: %s", exc)

    def stop(self) -> None:
        self.running = False
        for conn in self._connections.values():
            try:
                conn.close()
            except Exception:
                pass
        if self.server_sock:
            self.server_sock.close()
