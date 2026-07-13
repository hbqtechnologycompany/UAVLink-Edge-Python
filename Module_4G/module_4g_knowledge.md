# 4G Module & Network Monitor Knowledge Base

## Tổng quan
Module `Module_4G` tại `Pi_CM5_DroneBridgeService` chịu trách nhiệm duy trì kết nối mạng an toàn và ổn định nhất cho hệ thống DroneBridge (truyền tải MAVLink/Video từ máy bay CM5 đến trạm điều khiển qua 4G/WiFi).

Kiến trúc định tuyến đang sử dụng là **Policy-Based Routing (PBR)**, giúp tách biệt hoàn toàn đường truyền của DroneBridge so với đường truyền mặc định của hệ điều hành.

---

## 1. Cơ chế hoạt động (Policy-Based Routing)
Thay vì sử dụng metric để xếp hạng giao diện mạng (như truyền thống), hệ thống tạo ra một bảng định tuyến riêng biệt (`table 100 dronebridge`).

*   **Tất cả các gói tin (traffic)** phát sinh từ ứng dụng DroneBridge đều mang nhãn (mark) là `0x01` (FWMARK).
*   **IP Rule:** Hệ thống được cấu hình rule: nếu bắt được gói tin có mark `0x01`, hãy tra cứu `table 100` để ưu tiên định tuyến.
*   **System Default (Main Table):** WiFi (`wlan0`) luôn được coi là Default Route cho hệ thống (để các công cụ như SSH vào CM5 hoạt động mượt mà mà không lo bị kẹt nếu 4G rớt mạng).

---

## 2. Hệ thống Tự Động Failover (Chuyển mạch dự phòng)
File `connection_manager.py` là bộ não theo dõi và quyết định:

*   Chạy vòng lặp định kỳ (mỗi `30s`) ping kiểm tra `8.8.8.8` trên cả 2 giao diện `wwan0` (4G) và `wlan0` (WiFi).
*   Nếu `wwan0` bình thường: Định cấu hình PBR `table 100` đẩy traffic qua 4G.
*   Nếu `wwan0` rớt, rơi máy, mất sóng, nhưng `wlan0` còn sống: Tự động đổi PBR `table 100` sang WiFi. Dữ liệu chuyến bay lập tức chuyển sang WiFi, pilot không mất sóng.

---

## 3. Quy hoạch mới: Quản lý Restart Hardware thông minh (Bảo vệ Phần Cứng)
Phiên bản trước tồn tại lỗi restart modem QMI (`dronebridge-4g-init.service`) liên tục mỗi 5 phút khi mất 4G. Gây tình trạng "spam" log hệ thống, sinh nhiệt lượng lớn, sốc điện từ, hao mòn IC điều khiển modem. 

Quy hoạch mới bổ sung **3 Tính năng an toàn phần cứng**:

### A. Exponential Backoff (Khởi động lại theo cấp số nhân)
Khi 4G mất mạng trọn vẹn và không thể phục hồi thay vì cố tình Power-Cycle lặp đi lặp lại một cách mù quáng, mỗi lần thất bại, thời gian chờ để thử lại sẽ tăng gấp đôi theo biến `self._reinit_count`:
*   Lần fail phần cứng 1: Chờ 5 phút.
*   Lần fail phần cứng 2: Chờ 10 phút.
*   Lần fail phần cứng 3: Chờ 20 phút.
*   Lần fail phần cứng 4: Chờ 40 phút.
*   Lần fail tiếp theo: Giới hạn tối đa (Max Delay) là **60 phút (1 giờ)**.

### B. Smart Cooling (Nhận thức trạng thái Drone)
Nếu Drone hiện đang được Fallback an toàn trên sóng WiFi (`wlan_ok = True`), hệ thống hiểu rằng *"Mọi việc vẫn trong tầm kiểm soát, không cần phải cứu 4G bằng mọi giá ngay lúc này"*.
*   Nếu đang có WiFi, thời gian cố gắng restart 4G bị ép **tối thiểu là 30 phút/1 lần** để phần cứng được nghỉ ngơi và tránh gây nhiễu sóng khi hoạt động ổn định trên diện hẹp.

### C. Cơ chế Giảm Tải Log (Quiet / Anti-Spam)
Thay vì spam syslog 20.000 dòng `WARNING 4G down` mỗi 12 giờ, hệ thống được tinh chỉnh để:
*   Bỏ qua việc xuất tin nhắn ra màn hình đối với các cảnh báo thông thường (chỉ ghi lúc rớt mạng thực sự tại ngưỡng hoặc ghi đè chẵn chục `[10, 30, 60]`). Dễ dàng debug bằng lệnh `journalctl -u dronebridge-netmon.service`.
*   Tự động reset lại toàn bộ các bộ đếm về `0` (Clear Counters/Backoff) khi hệ thống xác định Modem QMI đã hoạt động trở lại thành công và lấy được IP.

---

## 4. Các lệnh kiểm tra và Debug thủ công

Nếu bạn đang SSH trong CM5 và muốn kiểm tra 4G/WiFi:

1. **Xem Trạng thái Tóm Tắt Nhanh**:
   ```bash
   sudo /usr/bin/python3 /opt/dronebridge/Module_4G/connection_manager.py status
   ```

2. **Xem Trạng Thái Dịch Vụ**:
   ```bash
   sudo systemctl status dronebridge-netmon.service
   sudo systemctl status dronebridge-4g-init.service
   ```

3. **In Live Log (Realtime)**:
   ```bash
   journalctl -u dronebridge-netmon.service -f
   ```

4. **Kích hoạt Lại Modem Thủ Công**:
   ```bash
   sudo systemctl restart dronebridge-4g-init.service
   ```
