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

    def authenticate(self):
        if not self.secret_key and not self.load_secret():
            logger.error("No secret key found. Registration required.")
            return False

        if not self.connect():
            return False

        try:
            # Step 1: Send AUTH_INIT
            uuid_bytes = self.drone_uuid.encode('utf-8')
            packet = struct.pack("<BH", self.MSG_AUTH_INIT, len(uuid_bytes)) + uuid_bytes
            self.conn.sendall(packet)
            logger.info(f"Sent AUTH_INIT (UUID={self.drone_uuid})")

            # Step 2: Receive AUTH_CHALLENGE
            self.conn.settimeout(15)
            data = self.conn.recv(4096)
            if not data or data[0] != self.MSG_AUTH_CHALLENGE:
                logger.error("Invalid AUTH_CHALLENGE received")
                return False

            # Format: [TYPE:1][NONCE_LEN:2][NONCE:var][TIMEOUT:2]
            nonce_len = struct.unpack_from("<H", data, 1)[0]
            nonce = data[3 : 3 + nonce_len]
            logger.info("Received challenge")

            # Step 3: Compute HMAC
            # Combined Key = SHA256(Secret + Shared)
            combined_key_seed = (self.secret_key + self.shared_secret).encode('utf-8')
            combined_key = hashlib.sha256(combined_key_seed).hexdigest().encode('utf-8')
            
            # HMAC-SHA256(combined_key, nonce)
            signature = hmac.new(combined_key, nonce, hashlib.sha256).digest()
            timestamp = int(time.time())

            # Step 4: Send AUTH_RESPONSE
            # Format: [TYPE:1][UUID_LEN:2][UUID:var][HMAC_LEN:2][HMAC:32][TIMESTAMP:8][IP_LEN:2][IP:var]
            ip_bytes = b"" # Optional
            resp_packet = struct.pack("<BH", self.MSG_AUTH_RESPONSE, len(uuid_bytes)) + uuid_bytes
            resp_packet += struct.pack("<H", len(signature)) + signature
            resp_packet += struct.pack("<Q", timestamp)
            resp_packet += struct.pack("<H", len(ip_bytes)) + ip_bytes
            
            self.conn.sendall(resp_packet)
            logger.info("Sent AUTH_RESPONSE")

            # Step 5: Receive AUTH_ACK
            data = self.conn.recv(4096)
            if not data or data[0] != self.MSG_AUTH_ACK:
                logger.error("Invalid AUTH_ACK received")
                return False

            # Format: [TYPE:1][RESULT:1][ERR:1][WAIT:2][SK_LEN:2][SK:var][TOKEN_LEN:2][TOKEN:var][EXP:8][INT:2]
            result = data[1]
            if result != 0:
                err_code = data[2]
                logger.error(f"Authentication failed (Result={result}, Error={err_code})")
                return False

            offset = 5 # skip type, result, err, wait
            
            def read_str(d, off):
                l = struct.unpack_from("<H", d, off)[0]
                s = d[off+2 : off+2+l].decode('utf-8')
                return s, off + 2 + l

            _, offset = read_str(data, offset) # Skip NewSecretKey if present
            self.session_token, offset = read_str(data, offset)
            self.expires_at = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
            self.refresh_interval = struct.unpack_from("<H", data, offset)[0]

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
        while self.running:
            time.sleep(self.refresh_interval or 30)
            if not self.running:
                break
            # Logic for session refresh would go here
            # For now, just a placeholder as session management is complex
            pass

    def get_session_refresh_packet(self):
        if not self.session_token:
            return None
        # Format: [TYPE:1][TOKEN_LEN:2][TOKEN:var][UUID_LEN:2][UUID:var]
        token_bytes = self.session_token.encode('utf-8')
        uuid_bytes = self.drone_uuid.encode('utf-8')
        return struct.pack("<BH", self.MSG_SESSION_REFRESH, len(token_bytes)) + token_bytes + \
               struct.pack("<H", len(uuid_bytes)) + uuid_bytes
