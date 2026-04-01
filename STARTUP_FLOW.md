# UAVLink-Edge Startup Flow

> Phiên bản: 5.0 — Cập nhật theo code thực tế (`main.py`)
> Cập nhật: 2026-04-01

---

## Toàn cảnh hệ thống (Boot Order)

```
Pi CM5 Boot
    │
    └─ UAVLink-Edge.service (user pi)
        └─ python main.py → MAVLink + Authentication (WiFi-only mode)
```

*(Lưu ý: Chế độ 4G và PBR routing đã được gỡ bỏ theo cấu trúc mới, hệ thống chỉ chạy WiFi-only)*

---

## Startup Flow — `main.py` (UAVLink-Edge Python)

```
┌─────────────────────────────────────────────────────────────────────┐
│  python main.py                                                     │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  Parse/Load Config           │
              │  cfg = Config("config.yaml") │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────────────────┐
              │  Initialize Components                   │
              │  auth = AuthClient(...)                  │
              │  fwd = Forwarder(cfg, auth)              │
              └──────────────────────┬───────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────┐
              │  STEP 1: Start Web Server                │
              │  start_server(port, fwd.stats, auth)     │
              └──────────────────────┬───────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────┐
              │  STEP 2: Authenticate với server         │
              │  auth.start()                            │
              │    → connect() & authenticate() [TCP]    │
              │    → go keepalive_loop (Thread)          │
              └──────────────────────┬───────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────┐
              │  STEP 3: Start Forwarder                 │
              │  fwd.start()                             │
              │    → start_listener (Kết nối Pixhawk)    │
              │    → go uplink_loop (Thread)             │
              │    → go downlink_loop (Thread)           │
              │    → go heartbeat_loop (Thread)          │
              └──────────────────────┬───────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────┐
              │  ✅ FULLY OPERATIONAL                    │
              │                                          │
              │  while True:                             │
              │      time.sleep(1)                       │
              └──────────────────────┬───────────────────┘
                                     │ Ctrl+C / SIGTERM
                                     ▼
              ┌──────────────────────────────────────────┐
              │  Graceful Shutdown                       │
              │  signal_handler() → sys.exit(0)          │
              └──────────────────────────────────────────┘
```

---

## Threads (Luồng) chạy khi OPERATIONAL

| Thread | Module | Mô tả |
|---|---|---|
| `MainThread` | `main.py` | Giữ ứng dụng sống (`while True`) và bắt tín hiệu tắt (SIGINT/SIGTERM) |
| `web_server` | `web_server.py` | Chạy Flask/HTTP server xử lý API request trên định tuyến `/` |
| `keepalive_loop` | `auth_client.py` | Quản lý vòng đời Authentication Session (TCP) |
| `uplink_loop` | `forwarder.py` | Đọc MAVLink từ Pixhawk (`recv_match`) → Gửi lên Server (UDP) |
| `downlink_loop` | `forwarder.py` | Nhận MAVLink từ Server về qua UDP socket → Gửi lại Pixhawk (`write`) |
| `heartbeat_loop`| `forwarder.py` | Gửi gói `MSG_SESSION_REFRESH` (UDP) tới server mỗi giây để mở/chống timeout cổng NAT |

---

## Log Timeline — Khởi động thành công

```text
T+0.0s  [MAIN] INFO: 🚀 Starting UAVLink-Edge (Python Version) on Pi 5
T+0.0s  [MAIN] INFO: Network mode: WiFi-only (4G disabled per request)
T+0.0s  [MAIN] INFO: Configuration loaded successfully
T+0.1s  [MAIN] INFO: Authenticating via public TCP...
T+x.xs  [AuthClient] INFO: Loaded secret key from storage 
T+x.xs  [AuthClient] INFO: Sent AUTH_INIT (UUID=...)
T+x.xs  [AuthClient] INFO: Received challenge
T+x.xs  [AuthClient] INFO: Sent AUTH_RESPONSE
T+x.xs  [AuthClient] INFO: ✅ Authenticated! Session expires in ...s
T+x.xs  [MAIN] INFO: ✅ Successfully authenticated
T+x.xs  [Forwarder] INFO: Connecting to Pixhawk via ...
T+x.xs  [Forwarder] INFO: Forwarder started. Target: (...)
T+x.xs  [MAIN] INFO: UAVLink-Edge running. Press Ctrl+C to stop.
```

---

## Configuration & Behavior Notes

* Mọi cấu hình được đọc từ file `config.yaml` thông qua parser trong `config.py`.
* **Chế độ mạng**: Hệ thống hiện tại được kiến trúc thuần túy chạy trên WiFi-only theo yêu cầu refactor. Các bước PBR routing phức tạp và module quản lý 4G không tải trong code gốc (Python).
* **Kết nối Server**: Drone sử dụng TCP để xác thực (`AuthClient`) và lấy session token, sau đó dùng IP/Port (Target Host) và token đó cho mọi gói MAVLink UDP (góp phần giảm latency). Các UDP heartbeat tự động giữ kết nối cho endpoint của Drone với Router Backend.
