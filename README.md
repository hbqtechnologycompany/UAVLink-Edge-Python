# UAVLink-Edge (Python Version for Pi 5)

[Tiếng Việt](#tiếng-việt) | [English](#english)

**Repository:** [github.com/hbqtechnologycompany/UAVLink-Edge-Python](https://github.com/hbqtechnologycompany/UAVLink-Edge-Python)

---

## English

Python implementation of **UAVLink-Edge** — a MAVLink bridge between the flight controller (Pixhawk/Cube) and the **qcloudstation** fleet server at [http://qcloudcontrol.com/](http://qcloudcontrol.com/).

This release aligns the Python stack with **Pi_CM5_DroneBridgeService** (auth, MAVLink forwarding, camera/landing, web UI) while remaining **user-run**: no systemd install, no PBR tables — start with `./run.sh` or `venv/bin/python main.py`.

![Cloud Control Interface](images/pilot-ui.jpg)

### System block diagram

```text
┌────────────────┐      MAVLink      ┌──────────────────────┐   UDP/VPN (WiFi/4G) ┌───────────────────────────┐
│ Flight         │◄─────────────────►│   UAVLink-Edge-Python │◄───────────────────►│   qcloudstation Server    │
│ Controller     │ Serial / Ethernet │   (Raspberry Pi 5)    │                     │ (http://qcloudcontrol.com)│
└────────────────┘                   └──────────────────────┘                     └───────────────────────────┘
```

### What's new (2026-07 sync)

| Area | Updates |
|------|---------|
| **Auth & startup** | `REGISTER_INIT` v2 (`vehicle_type`, `model`); `cloud_egress.py` — short wait when no 4G modem (avoids 120s boot stall); session heartbeat in forwarder |
| **MAVLink** | `prefer_ethernet` path (Pi ↔ Pixhawk over ETH); partner heartbeat on shared UDP socket; GPS filter; custom msgs 42998/42999; `DRONEBRIDGE_STATUS` |
| **Camera / landing** | `camera_mavlink.py` (VIDEO_STREAM_STATUS/INFORMATION); `landing_mavlink.py` (LANDING_TARGET uplink); `Find_landing/` processing stack |
| **VPN** | Re-provision on UUID mismatch; tolerate existing `uavlink0` interface |
| **Web UI** | App-shell layout (`dashboard.html`, `connect.html`, `settings.html`, `mavlink.html`); APIs: `/api/network/mode`, `/api/camera/*`, hardware settings |
| **4G (optional)** | Bundled `Module_4G/` for connection manager when netmon is present; works without 4G on WiFi-only setups |
| **Run helper** | `run.sh` — always uses project `venv` (fixes `sudo python` / system Python missing pymavlink) |

### Core components

1. **Auth Client (`auth_client.py`)** — HMAC-SHA256 TCP handshake, session token, keepalive, VPN provision request.
2. **MAVLink Forwarder (`forwarder.py`)** — Serial, TCP, UDP, or **Ethernet** to Pixhawk; uplink/downlink to fleet server over UDP (often via WireGuard).
3. **Web Server (`web/server.py`)** — Flask API + static Control Center on port **8080**.
4. **Cloud egress (`cloud_egress.py`)** — Reads `/run/dronebridge/network_status.json` when netmon runs; skips long 4G wait in manual mode.
5. **Camera / landing bridges** — MAVLink video stream status and precision-landing target injection.

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/hbqtechnologycompany/UAVLink-Edge-Python.git
cd UAVLink-Edge-Python
python3 install.py    # apt deps + venv + pip
```

Or manually:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `config.yaml`

Key sections (see full file for camera, landing, VPN):

```yaml
auth:
  uuid: "YOUR-DRONE-UUID"
  shared_secret: "YOUR-SHARED-SECRET"   # request: hbqsolution@gmail.com
  vehicle_type: 0
  model: ""

mavlink:
  connection_type: prefer_ethernet   # serial | udp_listen | prefer_ethernet

ethernet:
  local_ip: "10.41.10.10"
  pixhawk_ip: "10.41.10.2"
  pixhawk_port: 14550

vpn:
  enabled: true
  server_endpoint: YOUR_SERVER:51820
  router_vpn_ip: 10.8.0.1
```

### 3. Register (first time)

```bash
./run.sh --register
# or: ./venv/bin/python main.py --register
```

Secret is saved to `.drone_secret` (gitignored).

### 4. Run

```bash
./run.sh
# VPN / wg-quick may need root on first provision:
sudo ./run.sh
```

**Dashboard:** `http://<PI_IP>:8080/` → redirects to Control Center  
**MAVLink stats:** `http://<PI_IP>:8080/mavlink.html`  
**API:** `http://<PI_IP>:8080/api/status`

### Coexistence with DroneBridge (Go)

If **DroneBridge Go** was installed via `/opt/dronebridge`, disable autostart to avoid port **8080** conflicts and stale metrics:

```bash
sudo systemctl disable --now dronebridge.service dronebridge-netmon.service dronebridge-4g-init.service
```

UAVLink-Edge-Python is intended to be started manually (or add your own systemd unit later).

---

## Directory structure

```text
UAVLink-Edge-Python/
├── main.py                 # Entry point (venv re-exec, startup flow)
├── run.sh                  # Wrapper: venv/bin/python main.py
├── auth_client.py          # Fleet authentication
├── forwarder.py            # MAVLink bridge + partner heartbeat
├── cloud_egress.py         # Netmon / cloud_ready wait (user-run friendly)
├── camera_mavlink.py       # VIDEO_STREAM MAVLink to GCS
├── landing_mavlink.py      # LANDING_TARGET uplink
├── mavlink_custom.py       # Custom messages + GPS filter
├── vpn_manager.py          # WireGuard provision & lifecycle
├── config.yaml             # Local configuration
├── Module_4G/              # Optional 4G connection manager
├── Find_landing/           # Camera capture, ArUco/H landing detection
├── web/
│   ├── server.py           # Flask routes
│   ├── network_mode.py     # Network mode API
│   ├── camera_service.py   # Camera / overlay control
│   └── static/             # dashboard, connect, settings, mavlink UI
├── AUTHENTICATION_PROTOCOL.md
├── STARTUP_FLOW.md
└── requirements.txt
```

---

## Authentication (summary)

1. Drone sends UUID (`0x01`) on TCP port **5770**.
2. Server returns challenge nonce (`0x02`).
3. Drone signs with HMAC key `SHA256(SecretKey + SharedSecret)` (`0x03`).
4. Server returns session token (`0x04`); UDP forwarding uses this token.

Details: [AUTHENTICATION_PROTOCOL.md](AUTHENTICATION_PROTOCOL.md), startup order: [STARTUP_FLOW.md](STARTUP_FLOW.md).

---

## Tiếng Việt

### Giới thiệu

**UAVLink-Edge-Python** là bridge MAVLink giữa Pixhawk và server **qcloudstation**, chạy trên Raspberry Pi 5. Bản cập nhật này đồng bộ tính năng với stack **Pi_CM5_DroneBridgeService** nhưng **không** cài systemd/PBR — người dùng tự chạy bằng `./run.sh`.

### Tính năng mới (đồng bộ 2026-07)

- **Khởi động & xác thực:** `REGISTER_INIT` v2; không kẹt 120s khi không có modem 4G (`cloud_egress.py`).
- **MAVLink:** Ưu tiên Ethernet Pi ↔ Pixhawk; partner heartbeat dùng chung socket UDP; lọc GPS; message tùy chỉnh.
- **Camera & landing:** Trạng thái luồng video qua MAVLink; `LANDING_TARGET` cho auto-landing; module `Find_landing/`.
- **Web UI:** Giao diện Control Center (dashboard, kết nối, cài đặt, thống kê MAVLink).
- **VPN:** WireGuard tự provision; xử lý interface đã tồn tại.
- **`run.sh`:** Luôn dùng đúng `venv` (tránh thiếu pymavlink khi `sudo python`).

### Chạy nhanh

```bash
git clone https://github.com/hbqtechnologycompany/UAVLink-Edge-Python.git
cd UAVLink-Edge-Python
python3 install.py
# Sửa config.yaml (uuid, shared_secret, ethernet, vpn)
./run.sh --register          # lần đầu
./run.sh                     # các lần sau
```

Mở trình duyệt: `http://<IP_PI>:8080/`

### Lưu ý khi đã cài DroneBridge Go

Tắt service tự khởi động để tránh chiếm port 8080:

```bash
sudo systemctl disable --now dronebridge.service dronebridge-netmon.service dronebridge-4g-init.service
```

### Liên hệ shared secret

Email: **hbqsolution@gmail.com**

---

## License / About

Project site: [hbqtechnologycompany.github.io/UAVLink-Edge-Python/](https://hbqtechnologycompany.github.io/UAVLink-Edge-Python/)
