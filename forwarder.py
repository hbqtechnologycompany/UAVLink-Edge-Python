import socket
import threading
import time
import logging
from pymavlink import mavutil
from typing import Optional

logger = logging.getLogger("Forwarder")

class Forwarder:
    def __init__(self, config, auth_client):
        self.config = config
        self.auth_client = auth_client
        self.running = False
        self.pixhawk_conn = None
        self.server_sock = None
        self.target_addr = (config.forwarding.get('target_host'), config.forwarding.get('target_port'))
        self.tcp_host = config.mavlink.get('tcp_host', '0.0.0.0')
        self.tcp_port = config.mavlink.get('tcp_port', 14540)
        
        self.stats = {
            'rawIn': 0,
            'accepted': 0,
            'outServer': 0,
            'dropErr': 0
        }
        self.stats_lock = threading.Lock()

    def start_listener(self):
        conn_type = self.config.mavlink.get('connection_type', 'serial')
        if conn_type == 'serial':
            port = self.config.mavlink.get('serial_port', '/dev/ttyAMA0')
            baud = self.config.mavlink.get('serial_baud', 57600)
            logger.info(f"Connecting to Pixhawk via Serial: {port}@{baud}")
            self.pixhawk_conn = mavutil.mavlink_connection(port, baud=baud)
        elif conn_type == 'tcp_listen':
            logger.info(f"Listening for Pixhawk via TCP port {self.tcp_port}")
            self.pixhawk_conn = mavutil.mavlink_connection(f"tcpin:{self.tcp_host}:{self.tcp_port}")
        elif conn_type == 'tcp_client':
            logger.info(f"Connecting to Pixhawk via TCP: {self.tcp_host}:{self.tcp_port}")
            self.pixhawk_conn = mavutil.mavlink_connection(f"tcp:{self.tcp_host}:{self.tcp_port}")
        else: # Default udpin
            logger.info(f"Listening for Pixhawk via UDP port {self.tcp_port}")
            self.pixhawk_conn = mavutil.mavlink_connection(f"udpin:0.0.0.0:{self.tcp_port}")
        
        return True

    def start(self):
        if not self.start_listener():
            return False
            
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.running = True
        
        # Start uplink thread (Pixhawk -> Server)
        threading.Thread(target=self.uplink_loop, daemon=True).start()
        # Start downlink thread (Server -> Pixhawk)
        threading.Thread(target=self.downlink_loop, daemon=True).start()
        # Start heartbeat thread (registration)
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        
        logger.info(f"Forwarder started. Target: {self.target_addr}")
        return True

    def uplink_loop(self):
        while self.running:
            try:
                msg = self.pixhawk_conn.recv_match(blocking=True, timeout=1.0)
                if msg is None:
                    continue
                
                raw_data = msg.get_msgbuf()
                with self.stats_lock:
                    self.stats['rawIn'] += 1
                
                # Check for heartbeat and other filtering (similar to Go logic)
                # For simplicity, we forward all valid frames
                if self.auth_client.session_token:
                    self.server_sock.sendto(raw_data, self.target_addr)
                    with self.stats_lock:
                        self.stats['outServer'] += 1
                else:
                    with self.stats_lock:
                        self.stats['dropErr'] += 1
                        
            except Exception as e:
                logger.error(f"Uplink error: {e}")
                time.sleep(1)

    def downlink_loop(self):
        # We need a bound port to receive from server?
        # Actually server returns to the source port of uplink.
        # But if we want to receive independently, we might need a separate socket.
        # Here we use the same server_sock.
        while self.running:
            try:
                data, addr = self.server_sock.recvfrom(4096)
                if addr == self.target_addr:
                    self.pixhawk_conn.write(data)
            except Exception as e:
                # server_sock is used for sending, might not be bound for receiving or 
                # might receive ICMP unreachable if server not responding yet.
                pass
            time.sleep(0.01)

    def heartbeat_loop(self):
        # Periodically send session refresh via UDP to register endpoint with server
        while self.running:
            packet = self.auth_client.get_session_refresh_packet()
            if packet:
                self.server_sock.sendto(packet, self.target_addr)
            time.sleep(1)

    def stop(self):
        self.running = False
        if self.pixhawk_conn:
            self.pixhawk_conn.close()
        if self.server_sock:
            self.server_sock.close()
