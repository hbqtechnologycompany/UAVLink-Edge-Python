# Phân Tích Module Hạ Cánh Tự Động (Auto_landing)

Tài liệu này cung cấp cái nhìn tổng quan về thư mục `Auto_landing` thuộc hệ thống DroneBridge, giải thích cơ chế nhận diện bãi đáp sử dụng thẻ (Fiducial Marker - ArUco), tích hợp các kỹ thuật lọc nhiễu, đánh giá sai số và độ tin cậy trước khi ra quyết định hạ cánh.

---

## 1. Mục Đích (Purpose)

*   **Precision Landing:** Cung cấp thông tin thị giác máy tính dùng Python để hỗ trợ quá trình tự động hạ cánh (Auto Landing) cho Drone. Thay vì chỉ sử dụng GPS (sai số 1-3m), camera dưới bụng sẽ khoanh vùng mục tiêu (ArUco Marker) ở khoảng cách gần để cung cấp độ lệch (offset X, Y) chính xác đến centimet.
*   **Reliability Assessment:** Đánh giá tính ổn định của mục tiêu (Stability) và độ tin cậy của việc nhận diện (Confidence) để ngăn chặn việc hạ cánh khi tín hiệu kém hoặc có vật cản.
*   **Video Telemetry:** Stream trực tiếp video cấu hình thấp từ camera về trạm điều khiển qua RTSP để Pilot giám sát.

---

## 2. Chức Năng Chi Tiết (Functions)

Module được chia thành 3 phần: Quản lý phần cứng (Hardware), Xử lý ảnh (Vision), và Truyền phát (Streaming).

### 2.2. Lớp Thuật Toán Xử Lý Ảnh (`find.py`)
Thuật toán vận hành qua các bước:
1.  **Nhận diện ArUco Marker (`detect_aruco`):** Sử dụng thư viện `cv2.aruco` tối ưu.
2.  **Lọc nhiễu & Làm mượt (Statistical Filtering):** 
    - Sử dụng **Median Filter** với cửa sổ 10 frames để loại bỏ các điểm ảnh nhảy (jitter).
    - Tính toán **Độ lệch chuẩn (Standard Deviation)** của tọa độ để xác định xem vị trí mục tiêu có đang bị rung lắc hay không.
3.  **Đánh giá Độ tin cậy (Confidence Scoring):** Tính toán tần suất xuất hiện của Marker trong 20 frames gần nhất. Nếu tần suất < 80%, hệ thống coi như không tin cậy.
4.  **Kiểm tra điều kiện hạ cánh (`ready_to_land`):** Chỉ sẵn sàng khi đồng thời thỏa mãn:
    - Độ tin cậy > 80%.
    - Sai số ổn định (Error Std < 10 pixels).
    - Mục tiêu đã nằm gần tâm màn hình (< 30 pixels).
5.  Đóng gói toàn bộ kết quả vào Dictionary Queue `result_queue`.

---

## 3. Hiện Trạng Hệ Thống (Current Status)

**Điểm mạnh:**
*   Phân chia luồng (Threading / Queue) linh hoạt giúp CPU tải ít hơn.
*   **Aruco Marker** mang lại độ ổn định cực cao so với nhận diện hình dạng tĩnh.
*   **Kỹ thuật đánh giá sai số mới** giúp drone không bị bay "loạng choạng" khi gặp nhiễu hình ảnh nhẹ.

**Hạn chế & Rủi ro:**
1.  **Software Encoding:** Pipe Gstreamer vẫn dùng CPU để nén video (`x264enc`).
2.  **Mất dấu hoàn toàn:** Nếu drone bay quá thấp hoặc marker bị che khuất hoàn toàn, logic tìm kiếm vẫn cần được cải thiện để "nhớ" vị trí cuối cùng lâu hơn.

---

## 4. Hướng Cải Tiến (Future Improvement)

Dưới đây là các định hướng nâng cấp để đưa module vào mức hoàn thiện công nghiệp.

### Cải tiến 1: Tối ưu Hardware Encoding (GStreamer)
Chuyển đổi Pipeline Gstreamer sang sử dụng phần cứng nén Video (V4l2) chuyên dụng trên Pi CM5 để giải phóng 50-70% CPU cho các việc khác.
*Thay đổi trong `camera_streamer.py`:*
```python
# OLD: x264enc (tốn CPU)
# f"x264enc bitrate={self.config['bitrate']} speed-preset=ultrafast tune=zerolatency ! "

# ĐỀ XUẤT MỚI: Dùng v4l2h264enc
pipeline = (
    f"fdsrc fd=0 ! "
    f"rawvideoparse width={width} height={height} format=bgr framerate={fps}/1 ! "
    f"videoconvert ! video/x-raw,format=I420 ! "
    f"v4l2h264enc extra-controls=\"controls,video_bitrate={self.config['bitrate']*1000}\" ! "
    f"h264parse ! "
    f"rtspclientsink location={rtsp_url} protocols=tcp"
)
```

### Cải tiến 2: Tính toán Toạ độ không gian chi tiết (Pose Estimation)
Mặc dù hệ thống đã sử dụng thành công Fiducial Markers (ArUco), hiện tại mới chỉ trích xuất toạ độ pixel 2D trên khung ảnh để chỉ hướng 4 phía `LEFT, RIGHT, UP, DOWN`. 
Để tối ưu hóa, có thể kết hợp Calibration Matrix của Camera với dữ liệu nhận diện `cv2.solvePnP` để trích xuất trực tiếp góc Roll, Pitch, và khoảng cách Z từ Drone xuống marker.

### Cải tiến 3: Đóng Gọi Output MAVLink Xuống DroneBridge Service
Để drone *thực sự bay vào vị trí chữ H*, Python app phải gửi Telemetry về lại lõi Golang của DroneBridge, từ đó Server Go biên dịch thành chuẩn `MAV_CMD_LAND_LOCAL` hoặc Message `LANDING_TARGET (ID: 149)` đẩy xuống cổng Serial Pixhawk.

*Đề xuất tiêm hàm xử lý vào kết thúc luồng `find.py`:*
```python
import requests
import math

def send_mavlink_landing_target(result, fov_x_rad=1.047, fov_y_rad=0.785, frame_w=640, frame_h=480):
    if not result.get('detected'):
        return
        
    offset_x = result['offset_x']
    offset_y = result['offset_y']
    
    # Tính ra độ lệch Radian
    angle_x = (offset_x / (frame_w / 2.0)) * (fov_x_rad / 2.0)
    angle_y = (offset_y / (frame_h / 2.0)) * (fov_y_rad / 2.0)
    
    # Đóng gói HTTP RESTful hoặc qua gRPC / Socket ném về app Golang "main.go"
    payload = {
        "angle_x": angle_x,
        "angle_y": angle_y,
        "distance": result.get('distance', 1.0)
    }
    
    try:
        # Gửi dữ liệu về Module Golang webserver qua API cục bộ
        requests.post("http://127.0.0.1:8080/api/landing/target", json=payload, timeout=0.1)
    except Exception:
        pass # Timeout thì bỏ qua chờ frame tiếp
```
Bằng cách này, DroneBridge Golang chỉ phải tập trung Forward MAVLink, trong khi việc xử lý hình ảnh nặng nề do Python xử lý độc lập hoàn toàn.
---

## 5. Cấu Trúc Codebase (Codebase Structure)

Thư mục `Auto_landing` được thiết kế theo dạng module hóa để dễ dàng bảo trì và tích hợp:

| File | Chức năng |
| :--- | :--- |
| `camera_manager.py` | Quản lý vòng đời camera (Mở/Đóng, Cấu hình). Tự động chọn driver phù hợp cho Pi CM5 (Libcamera) hoặc USB (OpenCV). |
| `find.py` | **Lõi xử lý chính.** Chứa logic nhận diện ArUco, các bộ lọc thống kê (Statistical Filters) và đánh giá độ an toàn cho việc hạ cánh. |
| `camera_streamer.py` | Module Streaming. Lấy hình ảnh từ `camera_manager` và đẩy lên RTSP Server (MediaMTX) kèm theo các hình vẽ Overlay từ kết quả nhận diện. |
| `test_find.py` | Script chạy thử nghiệm độc lập, hỗ trợ giao diện người dùng (UI) để quan sát các chỉ số thời gian thực. |
| `camera_config.json` | Chứa các thông số phần cứng như độ phân giải, FPS, và loại Driver camera. |

---

## 6. Hướng Dẫn Tích Hợp (Integration Guide)

Để tích hợp module này vào một hệ thống lớn hơn (như Robot điều khiển hoặc Drone API), thực hiện theo các bước sau:

### Bước 1: Khởi chạy module nhận diện
Sử dụng hàm `main` trong `find.py` với chế độ chạy liên tục (`continuous_mode=True`) và truyền vào một hàm `callback`.

```python
from Auto_landing import find

def my_navigation_callback(data):
    if data['ready_to_land']:
        print("Mục tiêu ổn định! Đang thực hiện lệnh hạ cánh...")
        # Gửi lệnh hạ cánh xuống Drone
    elif data['detected']:
        print(f"Đang bám mục tiêu: Offset X={data['offset_x']}, Y={data['offset_y']}")
    else:
        print("Đang tìm kiếm...")

# Chạy module trong một Thread riêng hoặc quy trình chính
find.main(show_ui=True, continuous_mode=True, callback_func=my_navigation_callback)
```

### Bước 2: Hiểu cấu trúc dữ liệu trả về
Hàm callback sẽ nhận được một Dictionary chứa các thông số quan trọng:

*   `detected` (bool): Có nhìn thấy Marker hay không.
*   `offset_x`, `offset_y` (int): Độ lệch pixels của Marker so với tâm camera (đã qua lọc nhiễu).
*   `confidence` (float): Từ 0.0 đến 1.0. Tỷ lệ nhận diện thành công trong các frame gần nhất.
*   `is_stable` (bool): Tọa độ mục tiêu có đang đứng yên hay bị rung lắc.
*   `ready_to_land` (bool): **Đây là biến quan trọng nhất.** True khi mục tiêu đã vào tâm, ổn định và có độ tin cậy cao.
*   `direction` (str): Hướng di chuyển gợi ý (LEFT, RIGHT, CENTER, v.v.).

### Bước 3: Cấu hình hệ thống
Đảm bảo dependencies đã được cài đặt:
```bash
pip install opencv-contrib-python numpy
```
*Lưu ý: Phải dùng phiên bản `contrib` để có hỗ trợ đầy đủ ArUco.*
