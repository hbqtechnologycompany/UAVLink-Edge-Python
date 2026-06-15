import socket
import struct
import hmac
import hashlib
import time
import json
import os
import logging
import threading
from typing import Optional, Tuple

logger = logging.getLogger("AuthClient")

class AuthClient:
    MSG_AUTH_INIT = 0x01
    MSG_AUTH_CHALLENGE = 0x02
    MSG_AUTH_RESPONSE = 0x03
    MSG_AUTH_ACK = 0x04
    MSG_SESSION_REFRESH = 0x12

    def __init__(self, host: str, port: int, drone_uuid: str, shared_secret: str, keepalive_interval: int):
        self.host = host
        self.port = port
        self.drone_uuid = drone_uuid
        self.shared_secret = shared_secret
        self.secret_key = ""
        self.session_token = ""
        self.expires_at = 0
        self.refresh_interval = keepalive_interval
        self.conn: Optional[socket.socket] = None
        self.running = False
        self.lock = threading.Lock()
        
    def load_secret(self):
        secret_file = ".drone_secret"
        if not os.path.exists(secret_file) and os.path.exists("../.drone_secret"):
            secret_file = "../.drone_secret"
            
        if os.path.exists(secret_file):
            with open(secret_file, 'r') as f:
                data = json.load(f)
                self.secret_key = data.get("secret_key", "")
                logger.info("Loaded secret key from storage")
                return True
        return False

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

        ip_bytes = b""
        resp_packet = struct.pack("<BH", self.MSG_AUTH_RESPONSE, len(uuid_bytes)) + uuid_bytes
        resp_packet += struct.pack("<H", len(signature)) + signature
        resp_packet += struct.pack("<Q", timestamp)
        resp_packet += struct.pack("<H", len(ip_bytes)) + ip_bytes
        self.conn.sendall(resp_packet)
        logger.info("Sent AUTH_RESPONSE")

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
            return True

        except Exception as e:
            logger.error(f"Authentication error: {e}")
            if self.conn:
                self.conn.close()
                self.conn = None
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
                        self.conn.sendall(packet)
                        logger.info("Session refreshed")
                        # Gia hạn thời gian hết hạn ở local dựa trên refresh_interval
                        self.expires_at = time.time() + (self.refresh_interval if self.refresh_interval > 30 else 60)
                    except Exception as e:
                        logger.error(f"Failed to refresh session: {e}")
                        self.expires_at = 0 # Ép re-authenticate ở chu kỳ lặp sau
            
            time.sleep(1)

    def get_session_refresh_packet(self):
        if not self.session_token:
            return None
        # Format: [TYPE:1][TOKEN_LEN:2][TOKEN:var][UUID_LEN:2][UUID:var]
        token_bytes = self.session_token.encode('utf-8')
        uuid_bytes = self.drone_uuid.encode('utf-8')
        return struct.pack("<BH", self.MSG_SESSION_REFRESH, len(token_bytes)) + token_bytes + \
               struct.pack("<H", len(uuid_bytes)) + uuid_bytes
    def register(self):
        """Bootstrap drone credentials via AUTH_INIT (server no longer supports 0x10/0x11)."""
        if not self.connect():
            return False

        try:
            if not self._auth_handshake(bootstrap=True):
                logger.error("Registration handshake failed")
                return False

            new_secret = self.secret_key or self.session_token
            if not new_secret:
                logger.error("No SecretKey received from server")
                return False

            with open(".drone_secret", "w") as f:
                json.dump({"secret_key": new_secret}, f)

            self.secret_key = new_secret
            logger.info("✅ Registration successful. SecretKey saved to .drone_secret")
            return True

        except Exception as e:
            logger.error(f"Registration error: {e}")
            return False
        finally:
            if self.conn:
                self.conn.close()
                self.conn = None
