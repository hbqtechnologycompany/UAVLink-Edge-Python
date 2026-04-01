# Giao thức Xác thực & Truyền thông UAVLink-Edge

Tài liệu này chi tiết về quy trình bắt tay (handshake) xác thực bảo mật, quản lý phiên và cơ chế chuyển tiếp dữ liệu được sử dụng trong **UAVLink-Edge**.

---

## 🔐 1. Quy trình bắt tay xác thực (HMAC hai lớp)

Quá trình xác thực là một quy trình bắt tay thử thách-phản hồi (challenge-response) gồm 4 bước qua **TCP công cộng (mặc định port: 5770)**. Điều này đảm bảo việc quản lý phiên độc lập với luồng dữ liệu telemetry UDP.

### 🔄 Giản đồ luồng bắt tay (Handshake Flow)

```mermaid
sequence_diagram
    participant Drone as UAVLink-Edge (Drone)
    participant Server as Fleet Router (Server)

    Note over Drone: Load UUID & Private Secret
    Drone->>Server: MsgAuthInit (0x01) [DroneUUID]
    
    Server-->>Drone: MsgAuthChallenge (0x02) [Nonce, Timeout]
    
    Note over Drone: Compute CombinedKey = SHA256(Secret + Shared)
    Note over Drone: Compute HMAC = HMAC-SHA256(CombinedKey, Nonce)
    
    Drone->>Server: MsgAuthResponse (0x03) [DroneUUID, HMAC, Timestamp]
    
    Note over Server: Kiểm tra chữ ký & Thời gian
    Server-->>Drone: MsgAuthAck (0x04) [Result, SessionToken, ExpiresAt, Interval]
    
    Note over Drone: Thiết lập phiên thành công!
```

### 📦 Chi tiết các gói tin bắt tay

| Loại | Tên | Hướng | Cấu trúc dữ liệu (Payload) |
| :--- | :--- | :--- | :--- |
| **0x01** | `AUTH_INIT` | Drone → Server | `[UUID_LEN:2][UUID:var]` |
| **0x02** | `AUTH_CHALLENGE` | Server → Drone | `[NONCE_LEN:2][NONCE:var][TIMEOUT:2]` |
| **0x03** | `AUTH_RESPONSE` | Drone → Server | `[UUID_LEN:2][UUID:var][HMAC:32][TIMESTAMP:8][IP_LEN:2][IP:var]` |
| **0x04** | `AUTH_ACK` | Server → Drone | `[RESULT:1][ERR:1][SK_LEN:2][SK:var][TOKEN_LEN:2][TOKEN:var][EXP:8][INT:2]` |

### 🛠️ Logic bảo mật HMAC
1.  **Combined Key (Khóa kết hợp)**: `SHA256(secret_key + shared_secret)`. Điều này bảo vệ khóa bí mật ngay cả khi shared secret (dùng để đăng ký ban đầu) bị lộ.
2.  **Challenge-Response (Thử thách-Phản hồi)**: Server cung cấp một `Nonce` duy nhất. Drone PHẢI ký vào `Nonce` này bằng khóa kết hợp. Điều này ngăn chặn các cuộc tấn công phát lại (replay attack).
3.  **Timestamp (Dấu thời gian)**: Đóng vai trò là lớp phòng thủ thứ hai; các chữ ký cũ hơn 60 giây sẽ bị từ chối.

---

## 🛸 2. Cơ chế chuyển tiếp dữ liệu & Duy trì phiên

Sau khi xác thực, drone sẽ duy trì một phiên làm việc và chuyển tiếp dữ liệu telemetry MAVLink.

### 📡 Uplink (MAVLink → Server)
1.  **UDP In/Serial**: Nhận các khung MAVLink thô từ Pixhawk (UDP:14540 hoặc UART).
2.  **UDP Out**: Chuyển tiếp các khung đến Server (UDP:14550) bằng **IP công cộng** tiêu chuẩn.
3.  **Registration Heartbeat**: Mỗi **1 giây**, drone gửi một gói `SESSION_REFRESH` (Msg 0x12) qua **UDP** để đăng ký cổng nguồn (source port) và IP công cộng hiện tại với server.

### 📦 Gói tin Session Heartbeat (0x12)
Được sử dụng để xuyên thủng NAT (NAT traversal) và theo dõi drone phía server:
`[TYPE:1][TOKEN_LEN:2][TOKEN:var][UUID_LEN:2][UUID:var]`

### 📡 Downlink (Server → MAVLink)
1.  Server gửi các khung MAVLink đến **IP/Port nguồn** đã được đăng ký bởi gói heartbeat.
2.  UAVLink-Edge kiểm tra dữ liệu và chuyển tiếp đến Pixhawk thông qua endpoint MAVLink cục bộ.

---

## 🔍 3. Tóm tắt quy trình vòng đời (Life-Cycle)

| Hành động | Giao thức | Thời điểm | Mục đích |
| :--- | :--- | :--- | :--- |
| **Bắt tay** | TCP | Khởi động / Đổi IP | Thiết lập `SessionToken`. |
| **Làm mới (Refresh)** | TCP | Mỗi 30-90 giây | Gia hạn thời gian sống của `SessionToken`. |
| **Nhịp tim (Heartbeat)** | UDP | Mỗi 1 giây | Báo cho server biết endpoint UDP hiện tại (NAT). |
| **Telemetry** | UDP | Liên tục | Chuyển tiếp MAVLink hai chiều. |

---

## 🚫 4. Lưu ý về bảo mật
- **Không gửi bản rõ**: Mật khẩu và các khóa bí mật không bao giờ được gửi đi. Chỉ các chữ ký HMAC-SHA256 và UUID được truyền tải.
- **Tính độc lập**: Việc mất phiên TCP (xác thực) sẽ KHÔNG ngay lập tức dừng luồng telemetry UDP nếu server cho phép một khoảng thời gian ân hạn, nhưng phiên phải được gia hạn trước khi hết hạn.
- **Tối giản**: Kết nối là trực tiếp; không yêu cầu VPN (WireGuard) cho việc xác thực hoặc chuyển tiếp dữ liệu trong kiến trúc mới.
