import cv2
import numpy as np
from camera_manager import get_camera_manager
import time
from collections import deque
from threading import Thread
from queue import Queue

import random


detection_history = deque(maxlen=10)
frame_queue = Queue(maxsize=1)
result_queue = Queue(maxsize=1)
running = True
frame_skip_counter = 0
FRAME_SKIP = 3
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
            
    binary_image = gray # Với Aruco không cần xuất binary mask
    return results, output_image, binary_image

def capture_thread(cam_manager, camera_id, user_id):
    global running, frame_skip_counter
    while running:
        try:
            frame_skip_counter += 1
            if frame_skip_counter < FRAME_SKIP:
                time.sleep(0.01)
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
                        'similarity': 1.0, # Aruco có độ tin cậy rất cao
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


def main():
    
    global running
    
    camera_id = 0
    user_id = "Aruco_Test"
    
    try:
        cam_manager = get_camera_manager()
        camera_config = {'format': 'RGB888', 'size': (640, 480)}
        
        camera = cam_manager.get_camera(camera_id, user_id, camera_config)
        
        capture_worker = Thread(target=capture_thread, args=(cam_manager, camera_id, user_id), daemon=True)
        detection_worker = Thread(target=detection_thread, daemon=True)
        
        capture_worker.start()
        detection_worker.start()
        time.sleep(0.5)
        
        cv2.namedWindow('H Detection', cv2.WINDOW_NORMAL)
        cv2.namedWindow('Binary', cv2.WINDOW_NORMAL)
        
        frame_count = 0
        
        while True:
            if not result_queue.empty():
                results, output_image, binary_image, original_frame = result_queue.get()
                
                detection_history.append(len(results) > 0)
                stable_detection = sum(detection_history) >= 7
                
                frame_height, frame_width = output_image.shape[:2]
                screen_center_x = frame_width // 2
                screen_center_y = frame_height // 2
                
                cv2.line(output_image, (screen_center_x - 30, screen_center_y), 
                        (screen_center_x + 30, screen_center_y), (255, 0, 0), 2)
                cv2.line(output_image, (screen_center_x, screen_center_y - 30), 
                        (screen_center_x, screen_center_y + 30), (255, 0, 0), 2)
                
                
                if len(results) > 0 and stable_detection:
                    result = results[0]
                    x, y, w, h = result['bbox']
                    similarity = result['similarity']
                    
                    h_center_x = x + w // 2
                    h_center_y = y + h // 2
                    
                    cv2.line(output_image, (h_center_x - 20, h_center_y), 
                            (h_center_x + 20, h_center_y), (0, 0, 255), 3)
                    cv2.line(output_image, (h_center_x, h_center_y - 20), 
                            (h_center_x, h_center_y + 20), (0, 0, 255), 3)
                    cv2.circle(output_image, (h_center_x, h_center_y), 8, (0, 0, 255), -1)
                    
                    if 'circle_center' in result:
                        cv2.putText(output_image, "Landing Area Found!", (10, 30), 
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 255), 2)
                    
                    offset_x = h_center_x - screen_center_x
                    offset_y = h_center_y - screen_center_y
                    
                    cv2.line(output_image, (h_center_x, h_center_y), 
                            (screen_center_x, screen_center_y), (0, 255, 255), 3)
                    
                    direction = ""
                    if abs(offset_x) > 20:
                        direction += "RIGHT " if offset_x > 0 else "LEFT "
                    if abs(offset_y) > 20:
                        direction += "DOWN" if offset_y > 0 else "UP"
                    
                    if direction:
                        cv2.putText(output_image, f"Move: {direction}", (10, 60), 
                                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                        cv2.putText(output_image, f"X={offset_x:+.0f} Y={offset_y:+.0f}", 
                                       (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                else:
                    info_text = "Searching..."
                    cv2.putText(output_image, info_text, (10, 30), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                
                cv2.putText(output_image, f"FPS: {frame_count}", (frame_width - 120, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                
                cv2.imshow('H Detection', output_image)
                cv2.imshow('Binary', binary_image)
                
                frame_count += 1
            
            key = cv2.waitKey(1) & 0xFF
            
            if key == 27:
                print("\nStopping...")
                break

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        running = False
        time.sleep(0.2)
        print("Cleaning up...")
        cv2.destroyAllWindows()
        if 'cam_manager' in locals():
            cam_manager.release_camera(camera_id, user_id)
        print("Done!")


if __name__ == "__main__":
    main()