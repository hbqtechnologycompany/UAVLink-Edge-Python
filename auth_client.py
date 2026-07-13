import socket
import struct
import hmac
import hashlib
import time
import json
import os
import logging
import threading
from pathlib import Path
from typing import Optional, Tuple

from auth_apikey import (
    RESULT_SUCCESS,
    api_key_error_message,
    parse_api_key_delete_ack,
    parse_api_key_response,
    parse_api_key_revoke_ack,
    parse_api_key_status_response,
    serialize_api_key_delete,
    serialize_api_key_request,
    serialize_api_key_revoke,
    serialize_api_key_status,
)
from metrics import global_metrics

logger = logging.getLogger("AuthClient")

class AuthClient:
    MSG_AUTH_INIT = 0x01
    MSG_AUTH_CHALLENGE = 0x02
    MSG_AUTH_RESPONSE = 0x03
    MSG_AUTH_ACK = 0x04
    MSG_SESSION_REFRESH = 0x12
    MSG_REGISTER_INIT = 0xA0
    MSG_REGISTER_CHALLENGE = 0xA1
    MSG_REGISTER_RESPONSE = 0xA2
    MSG_REGISTER_ACK = 0xA3
    MSG_VPN_PROVISION_REQUEST = 0xB0
    MSG_VPN_PROVISION_ACK = 0xB1

    def __init__(self, host: str, port: int, drone_uuid: str, shared_secret: str, keepalive_interval: int):
        self.host = host
        self.port = port
        self.drone_uuid = drone_uuid
        self.shared_secret = shared_secret
        self.secret_key = ""
        self.api_key = ""
        self.session_token = ""
        self.expires_at = 0
        self.refresh_interval = keepalive_interval
        self.vehicle_type = 0
        self.model = ""
        self.conn: Optional[socket.socket] = None
        self.running = False
        self.lock = threading.Lock()
        self.tcp_lock = threading.Lock()
        
    def _secret_path(self) -> str:
        if os.path.exists(".drone_secret"):
            return ".drone_secret"
        if os.path.exists("../.drone_secret"):
            return "../.drone_secret"
        return ".drone_secret"

    def load_secret(self) -> bool:
        secret_file = self._secret_path()
        if not os.path.exists(secret_file):
            return False

        with open(secret_file, encoding="utf-8") as f:
            data = json.load(f)

        stored_uuid = str(data.get("uuid") or "").strip()
        if stored_uuid and stored_uuid != self.drone_uuid:
            logger.error(
                "Secret key thuộc UUID %s nhưng config.yaml là %s — chạy: python main.py --register",
                stored_uuid,
                self.drone_uuid,
            )
            return False

        self.secret_key = data.get("secret_key", "")
        self.api_key = str(data.get("api_key") or "")
        if not self.secret_key:
            return False
        logger.info("Loaded secret key from storage")
        return True

    def _save_secret_file(self, data: dict) -> None:
        secret_file = self._secret_path()
        with open(secret_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(secret_file, 0o600)

    def _persist_api_key(self, api_key: str, expires_at: int = 0) -> None:
        data = {"secret_key": self.secret_key, "uuid": self.drone_uuid}
        secret_file = self._secret_path()
        if os.path.exists(secret_file):
            with open(secret_file, encoding="utf-8") as f:
                data = json.load(f)
        data["api_key"] = api_key
        data["uuid"] = self.drone_uuid
        if expires_at:
            data["api_key_expires_at"] = expires_at
        self._save_secret_file(data)
        self.api_key = api_key

    def connect(self):
        try:
            self.conn = socket.create_connection((self.host, self.port), timeout=10)
            self.conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Linux specific TCP keepalive
            if hasattr(socket, "TCP_KEEPIDLE"):
                self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False

    def _local_ip_from_conn(self) -> str:
        if self.conn is not None:
            try:
                return self.conn.getsockname()[0]
            except OSError:
                pass
        try:
            from network_utils import get_local_ip
            return get_local_ip() or "0.0.0.0"
        except Exception:
            return "0.0.0.0"

    def _read_length_prefixed_string(self, data: bytes, offset: int) -> Tuple[str, int]:
        length = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        value = data[offset : offset + length].decode("utf-8")
        return value, offset + length

    def _parse_auth_ack(self, data: bytes) -> Tuple[str, str, int, int]:
        # Server ACK: [TYPE][RESULT][FIELD_LEN:2][FIELD:var]...[EXP:8][INT:2]
        if not data or data[0] != self.MSG_AUTH_ACK:
            raise ValueError("Invalid AUTH_ACK")
        if data[1] != 0:
            raise ValueError(f"Auth rejected (result={data[1]}, error={data[2]})")

        offset = 2
        first_field, offset = self._read_length_prefixed_string(data, offset)

        secret_key = ""
        session_token = first_field
        trailing = len(data) - offset
        if trailing > 10:
            next_len = struct.unpack_from("<H", data, offset)[0]
            if 0 < next_len <= 512 and offset + 2 + next_len + 10 <= len(data):
                secret_key = first_field
                session_token, offset = self._read_length_prefixed_string(data, offset)

        expires_at = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        refresh_interval = struct.unpack_from("<H", data, offset)[0]
        return secret_key, session_token, expires_at, refresh_interval

    def _hmac_key(self, bootstrap: bool = False) -> bytes:
        if bootstrap or not self.secret_key:
            return self.shared_secret.encode("utf-8")
        combined_seed = (self.shared_secret + self.secret_key).encode("utf-8")
        return hashlib.sha256(combined_seed).hexdigest().encode("utf-8")

    def _auth_handshake(self, bootstrap: bool = False) -> bool:
        uuid_bytes = self.drone_uuid.encode("utf-8")
        packet = struct.pack("<BH", self.MSG_AUTH_INIT, len(uuid_bytes)) + uuid_bytes
        self.conn.sendall(packet)
        logger.info(f"Sent AUTH_INIT (UUID={self.drone_uuid})")

        self.conn.settimeout(15)
        data = self.conn.recv(4096)
        if not data or data[0] != self.MSG_AUTH_CHALLENGE:
            logger.error("Invalid AUTH_CHALLENGE received")
            return False

        nonce_len = struct.unpack_from("<H", data, 1)[0]
        nonce = data[3 : 3 + nonce_len]
        logger.info("Received challenge")

        timestamp = int(time.time())
        message = f"{self.drone_uuid}:{nonce.hex()}:{timestamp}"
        signature = hmac.new(
            self._hmac_key(bootstrap=bootstrap),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        ip_str = self._local_ip_from_conn()
        ip_bytes = ip_str.encode("utf-8")
        resp_packet = struct.pack("<BH", self.MSG_AUTH_RESPONSE, len(uuid_bytes)) + uuid_bytes
        resp_packet += struct.pack("<H", len(signature)) + signature
        resp_packet += struct.pack("<Q", timestamp)
        resp_packet += struct.pack("<H", len(ip_bytes)) + ip_bytes
        self.conn.sendall(resp_packet)
        logger.info("Sent AUTH_RESPONSE (IP=%s)", ip_str)

        data = self.conn.recv(4096)
        if not data or data[0] != self.MSG_AUTH_ACK:
            logger.error("Invalid AUTH_ACK received")
            return False
        if data[1] != 0:
            if not bootstrap and self.secret_key:
                logger.info("Combined-key auth rejected, retrying with shared_secret")
                self.conn.close()
                self.conn = None
                if not self.connect():
                    return False
                return self._auth_handshake(bootstrap=True)
            logger.error(f"Authentication failed (result={data[1]}, error={data[2]})")
            return False

        secret_key, session_token, expires_at, refresh_interval = self._parse_auth_ack(data)
        if secret_key:
            self.secret_key = secret_key
        self.session_token = session_token
        self.expires_at = expires_at
        self.refresh_interval = refresh_interval
        global_metrics.set_auth_status("Authenticated")
        global_metrics.set_session_info(expires_at, refresh_interval)
        global_metrics.add_log("INFO", "Authenticated with fleet server")
        return True

    def authenticate(self):
        self.load_secret()

        if not self.connect():
            return False

        try:
            if not self._auth_handshake(bootstrap=not self.secret_key):
                return False

            logger.info(f"✅ Authenticated! Session expires in {self.expires_at - time.time():.0f}s")
            self.running = True
            global_metrics.set_auth_status("Authenticated")
            return True

        except Exception as e:
            logger.error(f"Authentication error: {e}")
            if self.conn:
                self.conn.close()
                self.conn = None
            return False

    def request_vpn_provision(self, vpn_manager) -> bool:
        """Request WireGuard config from router (0xB0) after authenticated session."""
        if not self.session_token:
            logger.error("[VPN] No session — authenticate first")
            return False
        if not self.conn:
            if not self.connect():
                return False

        try:
            priv, pub, is_new = vpn_manager.load_or_generate_keypair()
            if is_new:
                logger.info("[VPN] Generated new WireGuard keypair")

            uuid_b = self.drone_uuid.encode("utf-8")
            token_b = self.session_token.encode("utf-8")
            key_b = pub.encode("utf-8")
            packet = struct.pack("<B", self.MSG_VPN_PROVISION_REQUEST)
            packet += struct.pack("<H", len(uuid_b)) + uuid_b
            packet += struct.pack("<H", len(token_b)) + token_b
            packet += struct.pack("<H", len(key_b)) + key_b

            with self.tcp_lock:
                self.conn.sendall(packet)
                self.conn.settimeout(15)
                data = self.conn.recv(4096)
                self.conn.settimeout(None)

            if not data or data[0] != self.MSG_VPN_PROVISION_ACK:
                logger.error("[VPN] Invalid VPN provision response")
                return False
            if data[1] != 0:
                err = data[2] if len(data) > 2 else 0
                logger.error("[VPN] Provision rejected (error=0x%02x)", err)
                return False

            offset = 2
            assigned_ip, offset = self._read_length_prefixed_string(data, offset)
            server_pub, offset = self._read_length_prefixed_string(data, offset)
            server_ep, offset = self._read_length_prefixed_string(data, offset)

            vpn_manager.save_provisioned(
                priv, pub, assigned_ip, server_pub, server_ep, drone_uuid=self.drone_uuid
            )
            logger.info("[VPN] Provisioned IP=%s endpoint=%s", assigned_ip, server_ep)
            global_metrics.add_log("INFO", f"VPN provisioned: {assigned_ip}")
            return True
        except Exception as exc:
            logger.error("[VPN] Provision failed: %s", exc)
            return False

    def start(self):
        if self.authenticate():
            self.running = True
            threading.Thread(target=self.keepalive_loop, daemon=True).start()
            return True
        return False

    def keepalive_loop(self):
        backoff = 1
        while self.running:
            now = time.time()
            if now >= self.expires_at:
                logger.info("Session expired, re-authenticating")
                if self.conn:
                    self.conn.close()
                    self.conn = None
                
                if self.authenticate():
                    backoff = 1
                else:
                    logger.warning(f"Re-authentication failed, retrying in {backoff}s")
                    for _ in range(backoff):
                        if not self.running:
                            break
                        time.sleep(1)
                    backoff = min(backoff * 2, 60)
                    continue

            elif self.expires_at - now < 30:
                packet = self.get_session_refresh_packet()
                if packet and self.conn:
                    try:
                        with self.tcp_lock:
                            if self.conn:
                                self.conn.sendall(packet)
                        logger.info("Session refreshed")
                        self.expires_at = time.time() + (self.refresh_interval if self.refresh_interval > 30 else 60)
                    except Exception as e:
                        logger.error(f"Failed to refresh session: {e}")
                        self.expires_at = 0
            
            time.sleep(1)

    def get_session_refresh_packet(self):
        if not self.session_token:
            return None
        # Format: [TYPE:1][TOKEN_LEN:2][TOKEN:var][UUID_LEN:2][UUID:var]
        token_bytes = self.session_token.encode('utf-8')
        uuid_bytes = self.drone_uuid.encode('utf-8')
        return struct.pack("<BH", self.MSG_SESSION_REFRESH, len(token_bytes)) + token_bytes + \
               struct.pack("<H", len(uuid_bytes)) + uuid_bytes
    def set_registration_meta(self, vehicle_type: int = 0, model: str = "") -> None:
        self.vehicle_type = int(vehicle_type or 0) & 0xFF
        self.model = str(model or "")[:32]

    @staticmethod
    def _serialize_register_init(uuid: str, vehicle_type: int, model: str) -> bytes:
        uuid_bytes = uuid.encode("utf-8")
        model_bytes = model.encode("utf-8")[:32]
        packet = struct.pack("<BH", AuthClient.MSG_REGISTER_INIT, len(uuid_bytes)) + uuid_bytes
        if vehicle_type != 0 or model_bytes:
            packet += struct.pack("BB", vehicle_type & 0xFF, len(model_bytes)) + model_bytes
        return packet

    def register(self):
        """One-time registration via REGISTER_INIT (0xA0) — v2 when vehicle_type/model set."""
        if not self.shared_secret:
            logger.error("shared_secret is required for registration")
            return False
        if not self.connect():
            return False

        try:
            packet = self._serialize_register_init(self.drone_uuid, self.vehicle_type, self.model)
            self.conn.sendall(packet)
            if self.vehicle_type or self.model:
                logger.info(
                    "Sent REGISTER_INIT v2 (UUID=%s, vehicle_type=%d, model=%r)",
                    self.drone_uuid,
                    self.vehicle_type,
                    self.model,
                )
            else:
                logger.info("Sent REGISTER_INIT (UUID=%s)", self.drone_uuid)

            self.conn.settimeout(15)
            data = self.conn.recv(4096)
            if not data or data[0] != self.MSG_REGISTER_CHALLENGE:
                logger.error("Invalid REGISTER_CHALLENGE received")
                return False

            nonce_len = struct.unpack_from("<H", data, 1)[0]
            nonce = data[3 : 3 + nonce_len]
            logger.info("Received REGISTER_CHALLENGE")

            timestamp = int(time.time())
            message = f"{self.drone_uuid}:{nonce.hex()}:{timestamp}"
            signature = hmac.new(
                self.shared_secret.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).digest()

            uuid_bytes = self.drone_uuid.encode("utf-8")
            resp_packet = struct.pack("<BH", self.MSG_REGISTER_RESPONSE, len(uuid_bytes)) + uuid_bytes
            resp_packet += struct.pack("<H", len(signature)) + signature
            resp_packet += struct.pack("<Q", timestamp)
            self.conn.sendall(resp_packet)
            logger.info("Sent REGISTER_RESPONSE")

            data = self.conn.recv(4096)
            if not data or data[0] != self.MSG_REGISTER_ACK:
                logger.error("Invalid REGISTER_ACK received")
                return False
            if data[1] != 0:
                logger.error("Registration failed (result=%s, error=%s)", data[1], data[2] if len(data) > 2 else "?")
                return False

            offset = 2
            secret_key, offset = self._read_length_prefixed_string(data, offset)
            _session_token, offset = self._read_length_prefixed_string(data, offset)
            if not secret_key:
                logger.error("No SecretKey received from server")
                return False

            secret_file = self._secret_path()
            self._save_secret_file({"secret_key": secret_key, "uuid": self.drone_uuid})

            vpn_config = Path("vpn_config.json")
            if vpn_config.exists():
                vpn_config.unlink()
                logger.info("Removed %s — VPN sẽ được cấp lại cho UUID %s", vpn_config, self.drone_uuid)

            self.secret_key = secret_key
            self.session_token = ""
            self.expires_at = 0
            logger.info("Registration successful. SecretKey saved to %s", secret_file)
            global_metrics.add_log("INFO", f"Registered with fleet server (UUID={self.drone_uuid})")

            # API key (CLIENT API KEY) do fleet server cấp — lấy qua STATUS (0x24), không tạo mới (0x20).
            try:
                if self.authenticate():
                    self.running = True
                    state = self.sync_api_key_from_server()
                    if state.get("api_key"):
                        logger.info("API key received from fleet server")
                    elif state.get("status") == "backend_error":
                        logger.warning(
                            "Fleet backend chưa trả API key — kiểm tra drone trên Admin UI hoặc thử đồng bộ sau"
                        )
            except Exception as exc:
                logger.warning("Could not sync API key after registration: %s", exc)
            finally:
                self.running = False
                if self.conn:
                    self.conn.close()
                    self.conn = None

            return True

        except Exception as e:
            logger.error(f"Registration error: {e}")
            return False
        finally:
            if self.conn:
                self.conn.close()
                self.conn = None

    def _ensure_tcp_connection(self) -> bool:
        if self.conn is not None:
            return True
        return self.reconnect_tcp()

    def reconnect_tcp(self) -> bool:
        logger.info("[RECONNECT] Attempting to reconnect TCP to %s:%s", self.host, self.port)
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except OSError:
                    pass
                self.conn = None
        if not self.connect():
            return False
        global_metrics.set_ip(self.conn.getsockname()[0] if self.conn else "")
        logger.info("[RECONNECT] TCP reconnected successfully")
        return True

    def force_reconnect(self) -> None:
        with self.tcp_lock:
            if self.conn is not None:
                logger.info("[AUTH] ForceReconnect due to network change")
                try:
                    self.conn.close()
                except OSError:
                    pass
                self.conn = None

    def _exchange_tcp(self, packet: bytes, response_parser, timeout: float = 3.0):
        with self.tcp_lock:
            if not self.running:
                raise RuntimeError("auth client not running")
            if not self.session_token:
                raise RuntimeError("no active session")
            if not self._ensure_tcp_connection():
                raise RuntimeError("connection lost and reconnect failed")

            try:
                self.conn.sendall(packet)
                self.conn.settimeout(timeout)
                data = self.conn.recv(4096)
            except (TimeoutError, OSError) as exc:
                if self.reconnect_tcp():
                    self.conn.sendall(packet)
                    self.conn.settimeout(timeout)
                    data = self.conn.recv(4096)
                else:
                    raise RuntimeError(f"connection lost and reconnect failed: {exc}") from exc
            finally:
                if self.conn:
                    self.conn.settimeout(None)

            if not data:
                raise TimeoutError("no response from router")
            return response_parser(data)

    def get_api_key_status(self, retries: int = 3, retry_delay: float = 0.5) -> dict:
        last_error = None
        for attempt in range(retries):
            try:
                packet = serialize_api_key_status(self.drone_uuid, self.session_token)
                resp = self._exchange_tcp(packet, parse_api_key_status_response)
                return {
                    "has_active_key": resp.has_active_key == 0x01,
                    "status": resp.status,
                    "api_key": resp.api_key,
                    "created_at": resp.created_at,
                    "expires_at": resp.expires_at,
                    "user_uuid": resp.user_uuid,
                    "user_active_at": resp.user_activated_at,
                }
            except Exception as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(retry_delay)
        raise RuntimeError(str(last_error))

    def sync_api_key_from_server(self) -> dict:
        """Lấy CLIENT API KEY đã được fleet server cấp (MSG_API_KEY_STATUS 0x24)."""
        state = self.get_api_key_status()
        api_key = state.get("api_key") or ""
        if state.get("has_active_key") and api_key:
            self._persist_api_key(api_key, int(state.get("expires_at") or 0))
        return state

    def request_api_key(self, expiration_hours: int) -> dict:
        expiration_hours = max(1, min(720, int(expiration_hours)))
        packet = serialize_api_key_request(self.drone_uuid, self.session_token, expiration_hours)
        resp = self._exchange_tcp(packet, parse_api_key_response)
        if resp.result != RESULT_SUCCESS:
            if resp.error_code:
                raise RuntimeError(
                    f"API key request failed: {api_key_error_message(resp.error_code)}"
                )
            raise RuntimeError("API key request failed")
        global_metrics.add_log("INFO", "API key generated successfully")
        return {
            "api_key": resp.api_key,
            "expires_at": resp.expires_at,
        }

    def revoke_api_key(self) -> None:
        packet = serialize_api_key_revoke(self.drone_uuid, self.session_token)
        result, error_code = self._exchange_tcp(packet, parse_api_key_revoke_ack)
        if result != RESULT_SUCCESS:
            raise RuntimeError(f"API key revoke failed (error code: 0x{error_code:02x})")
        global_metrics.add_log("INFO", "API key revoked successfully")

    def delete_api_key(self) -> None:
        packet = serialize_api_key_delete(self.drone_uuid, self.session_token)
        result, error_code = self._exchange_tcp(packet, parse_api_key_delete_ack)
        if result != RESULT_SUCCESS:
            raise RuntimeError(f"API key delete failed (error code: 0x{error_code:02x})")
        global_metrics.add_log("INFO", "API key deleted successfully")
