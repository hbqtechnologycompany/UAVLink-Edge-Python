# UAVLink-Edge (Phiên bản Python cho Pi 5)

Dự án này là phiên bản clone viết bằng Python của UAVLink-Edge gốc (viết bằng Go). Phiên bản này được thiết kế tối giản, tập trung vào việc quản lý truyền tải dữ liệu MAVLink và xác thực bảo mật với Fleet Server, đặc biệt phù hợp cho các thiết bị như Raspberry Pi 5 hoạt động chủ yếu qua mạng WiFi.

## 🚀 1. Tổng quan Kiến trúc

Phiên bản Python này hoạt động với 3 thành phần (module) cốt lõi:

1.  **Auth Client (`auth_client.py`)**: Đảm nhiệm quá trình bắt tay (handshake) bằng HMAC-SHA256 qua TCP để xác thực danh tính Drone với máy chủ. Nó duy trì `SessionToken` và gửi các gói tin Heartbeat để giữ kết nối.
2.  **MAVLink Forwarder (`forwarder.py`)**: Lắng nghe và giao tiếp với mạch điều khiển bay (Pixhawk/Cube) thông qua cổng Serial (UART) hoặc TCP/UDP. Data nhận được sẽ được đóng gói và gửi lên Fleet Server qua giao thức UDP.
3.  **Web Server (`web_server.py`)**: Chạy một web API siêu nhẹ (Flask) ở port 8080 để giám sát trạng thái (`/api/status`) theo thời gian thực (real-time).

---

## 🛠️ 2. Hướng dẫn Môi trường và Triển khai (Deploy)

Để chạy hệ thống một cách tối ưu và không ảnh hưởng đến các thư viện Python của OS, chúng ta sẽ sử dụng Môi trường ảo (Virtual Environment - `venv`).

### Bước 1: Chuẩn bị mã nguồn và môi trường
```bash
# Clone source code
git clone <URL_REPO_CUA_BAN>
cd UAVLink-Edge/UAVLink-Edge-Python

# Tạo môi trường ảo (Khuyên dùng)
python3 -m venv venv

# Kích hoạt môi trường ảo
source venv/bin/activate
```

### Bước 2: Cài đặt các thư viện phụ thuộc (Dependencies)
```bash
# Đảm bảo pip đang được cập nhật
pip install --upgrade pip

# Cài đặt các thư viện cần thiết
pip install -r requirements.txt
```
*Ghi chú: Thư viện `pymavlink` là thành phần quan trọng nhất để phân tích và truyền tải gói tin MAVLink.*

### Bước 3: Cấu hình `config.yaml`
Bạn cần cấu hình lại file `config.yaml` nội bộ trong thư mục Python để phù hợp với phần cứng đang gắn trên Pi 5.
Mở file `config.yaml`:
```yaml
mavlink:
    connection_type: "serial"    # Hoặc "tcp_client", "udp_listen"
    serial_port: "/dev/ttyAMA0"  # Thay đổi cổng UART tương ứng trên Pi 5
    serial_baud: 57600           # Tốc độ baudrate của Serial
    
auth:
    uuid: "UUID-CUA-DRONE"
    shared_secret: "SECRET-KEY-DUNG-CHUNG"
```
*Lưu ý: Bạn cũng cần đảm bảo có file `.drone_secret` (nếu đã đăng ký) trong cùng thư mục hoặc ở thư mục cha.*

### Bước 4: Chạy ứng dụng
Do phiên bản này được thiết kế để chạy như một "process bình thường", không cần systemd service phức tạp:

```bash
# Chạy trực tiếp (để debug)
python3 main.py

# Hoặc chạy ẩn dưới background sử dụng nohup
nohup python3 main.py > uavlink.log 2>&1 &
```

Trạng thái hệ thống thiết bị có thể kiểm tra qua URL:
👉 `http://<IP_PI_5>:8080/api/status`

---

## 💻 3. Hướng dẫn Phát triển thêm (Development Guide)

Để các developer dễ dàng mở rộng và thêm các module mới vào dự án, cấu trúc mã nguồn được quy hoạch rõ ràng:

### Cổng MAVLink & Routing (`forwarder.py`)
- Mọi logic MAVLink đều gom vào class `Forwarder`.
- Nếu cần thêm các logic nội bộ (ví dụ: bọc thêm header, lọc tin nhắn MSG_ID nhất định không gửi lên server), hãy sửa hàm `uplink_loop()` và `downlink_loop()`.
- Biến `self.pixhawk_conn` giữ kết nối với Hardware. Mọi lệnh ghi (ví dụ: đổi tham số, ARM/DISARM) đều có thể gọi qua `self.pixhawk_conn.write(data)`.

### Tích hợp thêm API Web (`web_server.py`)
- Nếu bạn cần viết một app riêng tư để điều khiển tính năng cục bộ trên drone qua WiFi (ví dụ: Tool Calib ESC), hãy mở `web_server.py`.
- Thêm endpoint bằng thư viện Flask mặc định của module:
  ```python
  @app.route('/api/custom_action', methods=['POST'])
  def custom_action():
      # Viết code tương tác với Pixhawk tại đây
      return jsonify({"success": True})
  ```

### Quy trình Xác thực (Authentication)
Phiên bản Python này tuân thủ đúng quy trình bắt tay 4 bước như bản Go gốc (Do file tài liệu gốc đã bị xóa, đây là mô tả tóm tắt):
1.  **Gửi UUID**: Drone kết nối đến server TCP (Port 5770 mặc định) và gửi gói `0x01` kèm UUID.
2.  **Nhận Thử thách**: Server trả về gói `0x02` kèm `Nonce`.
3.  **Ký chữ ký**: Drone lấy mã khóa `SHA256(SecretKey + SharedSecret)` làm chìa khóa HMAC để mã hóa `Nonce`, kèm theo `Timestamp` thành gói `0x03` gửi ngược lại.
4.  **Nhận Token**: Server xác nhận chữ ký. Trả về gói `0x04` kèm thẻ `SessionToken`. Các tiến trình UDP kế tiếp sẽ dùng `SessionToken` này để chứng minh quyền hợp lệ.
Logic này được đóng gói gọn trong `auth_client.py`, dev **không nên** tự thay đổi phần mã hóa (hashlib/hmac) trừ khi Fleet Server có bản nâng cấp protocol mới.

## Cấu trúc thư mục

```
UAVLink-Edge-Python/
├── auth_client.py      # Module xác thực HMAC bảo mật an toàn
├── config.py           # Parser file YAML cho logic toàn hệ thống
├── config.yaml         # File cấu hình nội bộ trên máy
├── forwarder.py        # Module Bridge MAVLink bằng `pymavlink`
├── main.py             # Entrypoint chính của chương trình
├── README.md           # Tài liệu hướng dẫn bạn đang đọc
├── requirements.txt    # Danh sách thư viện PIP
└── web_server.py       # Mini API Framework (Flask)
```
