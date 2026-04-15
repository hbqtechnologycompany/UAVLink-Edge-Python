import cv2
import numpy as np
from camera_manager import get_camera_manager
import time
from collections import deque
from threading import Thread
from queue import Queue

import random


detection_history = deque(maxlen=20)   # Tỷ lệ xuất hiện target
offset_history_x = deque(maxlen=10)    # Lọc nhiễu tọa độ X
offset_history_y = deque(maxlen=10)    # Lọc nhiễu tọa độ Y
frame_queue = Queue(maxsize=1)
result_queue = Queue(maxsize=1)
running = True
frame_skip_counter = 0
FRAME_SKIP = 2 # Tăng tốc độ phản hồi
last_stable_result = None

def get_aruco_detector():
    try:
        # Cho OpenCV 4.7+
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        parameters = cv2.aruco.DetectorParameters()
        detector = cv2.aruco.ArucoDetector(dictionary, parameters)
        return detector
    except AttributeError:
        # Cho OpenCV < 4.7
        dictionary = cv2.aruco.Dictionary_get(cv2.aruco.DICT_4X4_50)
        parameters = cv2.aruco.DetectorParameters_create()
        return dictionary, parameters

def detect_aruco(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    
    detector = get_aruco_detector()
    if isinstance(detector, tuple):
        dictionary, parameters = detector
        corners, ids, rejected = cv2.aruco.detectMarkers(gray, dictionary, parameters=parameters)
    else:
        corners, ids, rejected = detector.detectMarkers(gray)
        
    results = []
    output_image = image.copy()
    
    if ids is not None and len(ids) > 0:
        cv2.aruco.drawDetectedMarkers(output_image, corners, ids)
        for i, corner in enumerate(corners):
            c = corner[0]
            center_x = int(np.mean(c[:, 0]))
            center_y = int(np.mean(c[:, 1]))
            
            x_min = int(np.min(c[:, 0]))
            y_min = int(np.min(c[:, 1]))
            x_max = int(np.max(c[:, 0]))
            y_max = int(np.max(c[:, 1]))
            
            width = x_max - x_min
            height = y_max - y_min
            
            results.append({
                'id': int(ids[i][0]),
                'bbox': (x_min, y_min, width, height),
                'center': (center_x, center_y),
                'corners': c
            })
            
    binary_image = gray
    return results, output_image, binary_image

def capture_thread(cam_manager, camera_id, user_id):
    global running, frame_skip_counter
    while running:
        try:
            frame_skip_counter += 1
            if frame_skip_counter < FRAME_SKIP:
                time.sleep(0.005)
                continue
            
            frame_skip_counter = 0
            frame = cam_manager.capture_frame(camera_id, user_id)
            
            if frame is not None:
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                if frame_queue.full():
                    try:
                        frame_queue.get_nowait()
                    except:
                        pass
                frame_queue.put(frame_bgr)
            else:
                time.sleep(0.01)
        except Exception as e:
            print(f"Capture error: {e}")
            time.sleep(0.05)

def detection_thread():
    global running
    while running:
        try:
            if not frame_queue.empty():
                frame = frame_queue.get()
                results, output_image, binary_image = detect_aruco(frame)
                
                formatted_results = []
                for res in results:
                    formatted_results.append({
                        'bbox': res['bbox'],
                        'center': res['center'],
                        'id': res['id'],
                        'similarity': 1.0,
                        'area': res['bbox'][2] * res['bbox'][3]
                    })

                if result_queue.full():
                    try:
                        result_queue.get_nowait()
                    except:
                        pass

                result_queue.put((formatted_results, output_image, binary_image, frame))
            else:
                time.sleep(0.001)
        except Exception as e:
            print(f"Detection error: {e}")
            time.sleep(0.01)

def calculate_reliability(history):
    if not history:
        return 0.0
    return sum(history) / len(history)

def main(show_ui=False, continuous_mode=False, callback_func=None):
    """
    Phát hiện landing pad và đánh giá sai số/độ tin cậy.
    """
    global running
    running = True
    
    camera_id = 0
    user_id = "Auto_Landing_Service"
    
    result_data = {
        'detected': False,
        'offset_x': 0,
        'offset_y': 0,
        'distance': 0.0,
        'target_position': (0, 0),
        'target_size': (0, 0),
        'confidence': 0.0,          # Độ tin cậy (0.0 - 1.0)
        'is_stable': False,         # Tọa độ có ổn định không
        'ready_to_land': False,     # Sẵn sàng hạ cánh (đã vào tâm + tin cậy cao)
        'error_std': 0.0,           # Độ lệch chuẩn của sai số
        'direction': 'NONE'
    }
    
    cam_manager = None
    
    try:
        cam_manager = get_camera_manager()
        camera_config = {'format': 'RGB888', 'size': (640, 480)}
        camera = cam_manager.get_camera(camera_id, user_id, camera_config)
        
        capture_worker = Thread(target=capture_thread, args=(cam_manager, camera_id, user_id), daemon=True)
        detection_worker = Thread(target=detection_thread, daemon=True)
        
        capture_worker.start()
        detection_worker.start()
        time.sleep(0.5)
        
        if show_ui:
            cv2.namedWindow('Auto Landing', cv2.WINDOW_NORMAL)
        
        while running:
            if not result_queue.empty():
                results, output_image, _, _ = result_queue.get()
                
                detected_this_frame = len(results) > 0
                detection_history.append(detected_this_frame)
                
                frame_height, frame_width = output_image.shape[:2]
                screen_center_x = frame_width // 2
                screen_center_y = frame_height // 2
                
                if show_ui:
                    # Vẽ tâm màn hình
                    cv2.drawMarker(output_image, (screen_center_x, screen_center_y), (255, 0, 0), cv2.MARKER_CROSS, 30, 2)
                
                confidence = calculate_reliability(detection_history)
                result_data['confidence'] = confidence
                
                if detected_this_frame:
                    target = results[0]
                    tx, ty = target['center']
                    tw, th = target['bbox'][2:4]
                    
                    # Tính toán offset thô
                    raw_offset_x = tx - screen_center_x
                    raw_offset_y = ty - screen_center_y
                    
                    # Lưu vào history để lọc nhiễu
                    offset_history_x.append(raw_offset_x)
                    offset_history_y.append(raw_offset_y)
                    
                    # Sử dụng Median Filter để loại bỏ jitter/outliers
                    smooth_offset_x = np.median(offset_history_x)
                    smooth_offset_y = np.median(offset_history_y)
                    
                    # Đánh giá sai số (Độ lệch chuẩn)
                    if len(offset_history_x) > 5:
                        error_std = (np.std(offset_history_x) + np.std(offset_history_y)) / 2.0
                        result_data['is_stable'] = error_std < 10.0 # Ngưỡng ổn định 10 pixels
                        result_data['error_std'] = float(error_std)
                    
                    distance = np.sqrt(smooth_offset_x**2 + smooth_offset_y**2)
                    
                    direction = ""
                    if abs(smooth_offset_x) > 15:
                        direction += "RIGHT " if smooth_offset_x > 0 else "LEFT "
                    if abs(smooth_offset_y) > 15:
                        direction += "DOWN " if smooth_offset_y > 0 else "UP "
                    if not direction:
                        direction = "CENTER"
                    
                    # Sẵn sàng hạ cánh khi: tự tin cao (>80%), tọa độ ổn định, và đã gần tâm (<30px)
                    is_centered = distance < 30
                    result_data['ready_to_land'] = confidence > 0.8 and result_data['is_stable'] and is_centered
                    
                    result_data['detected'] = True
                    result_data['offset_x'] = int(smooth_offset_x)
                    result_data['offset_y'] = int(smooth_offset_y)
                    result_data['distance'] = float(distance)
                    result_data['target_position'] = (int(screen_center_x + smooth_offset_x), int(screen_center_y + smooth_offset_y))
                    result_data['target_size'] = (tw, th)
                    result_data['direction'] = direction.strip()
                    
                    if show_ui:
                        color = (0, 255, 0) if result_data['ready_to_land'] else (0, 255, 255)
                        # Vẽ marker mục tiêu đã được làm mượt
                        tgt_pos = result_data['target_position']
                        cv2.circle(output_image, tgt_pos, 10, color, -1)
                        cv2.line(output_image, (screen_center_x, screen_center_y), tgt_pos, color, 2)
                        
                        # Hiển thị thông số đánh giá
                        y_pos = 30
                        status_texts = [
                            f"Confidence: {confidence*100:.1f}%",
                            f"Error Std: {result_data['error_std']:.2f}",
                            f"Status: {'STABLE' if result_data['is_stable'] else 'UNSTABLE'}",
                            f"Ready: {'YES' if result_data['ready_to_land'] else 'NO'}",
                            f"Move: {direction}"
                        ]
                        for txt in status_texts:
                            cv2.putText(output_image, txt, (10, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                            y_pos += 25
                else:
                    result_data['detected'] = False
                    result_data['is_stable'] = False
                    result_data['ready_to_land'] = False
                    if show_ui:
                        cv2.putText(output_image, "SEARCHING TARGET...", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                
                if show_ui:
                    cv2.imshow('Auto Landing', output_image)
                    if cv2.waitKey(1) & 0xFF == 27: break
                
                if callback_func:
                    callback_func(result_data.copy())
                
                if not continuous_mode and result_data['detected']:
                    break
            else:
                time.sleep(0.01)
                
    except Exception as e:
        print(f"Main Loop Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        running = False
        time.sleep(0.2)
        if show_ui: cv2.destroyAllWindows()
        if cam_manager: cam_manager.release_camera(camera_id, user_id)
    return result_data





