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


def preprocess_image(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(3, 3))
    enhanced = clahe.apply(gray)
    blur = cv2.GaussianBlur(enhanced, (3, 3), 0)
    edges = cv2.Canny(blur, 50, 150)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)
    return edges



def fit_circle(points):
    x = points[:, 0]
    y = points[:, 1]
    A = np.c_[2*x, 2*y, np.ones(points.shape[0])]
    b = x**2 + y**2
    c, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    a, b, c = c
    center = (a, b)
    radius = np.sqrt(c + a**2 + b**2)
    return center, radius

def ransac_ring(contour, n_iter=100, threshold=2.0, min_ring_width=10, max_ring_width=80):
    pts = contour.reshape(-1, 2)
    best_score = 0
    best_circle_out = None
    best_circle_in = None
    for _ in range(n_iter):
        if len(pts) < 6: break
        sample = random.sample(range(len(pts)), 6)
        sample_out = pts[sample[:3]]
        sample_in = pts[sample[3:]]
        center_out, r_out = fit_circle(sample_out)
        center_in, r_in = fit_circle(sample_in)
        dists_out = np.abs(np.sqrt((pts[:,0]-center_out[0])**2 + (pts[:,1]-center_out[1])**2) - r_out)
        dists_in = np.abs(np.sqrt((pts[:,0]-center_in[0])**2 + (pts[:,1]-center_in[1])**2) - r_in)
        score = np.sum(dists_out < threshold) + np.sum(dists_in < threshold)
        # Kiểm tra đồng tâm và độ dày
        center_dist = np.linalg.norm(np.array(center_out) - np.array(center_in))
        ring_width = abs(r_out - r_in)
        if score > best_score and center_dist < 5 and min_ring_width < ring_width < max_ring_width:
            best_score = score
            best_circle_out = (center_out, r_out)
            best_circle_in = (center_in, r_in)
    if best_circle_out and best_circle_in:
        return True, best_circle_out, best_circle_in
    return False, None, None

def detect_circles(image, min_circularity=0.65, min_area=8000, max_ellipse_ratio=2.5, min_points=30):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    denoised = cv2.bilateralFilter(enhanced, 5, 25, 25)
    _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    circles = []
    for cnt in contours:
        if len(cnt) < min_points:
            continue
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        peri = cv2.arcLength(cnt, True)
        if peri == 0:
            continue
        circularity = 4 * np.pi * area / (peri * peri)
        if circularity < min_circularity:
            continue

        # 1. Nhận diện ring bằng RANSAC
        is_ring, circle_out, circle_in = ransac_ring(cnt)
        if is_ring:
            cx, cy = map(int, circle_out[0])
            r_out = int(circle_out[1])
            r_in = int(circle_in[1])
            pad = int(r_out * 0.3)
            x0 = max(0, cx - r_out - pad)
            y0 = max(0, cy - r_out - pad)
            x1 = min(image.shape[1], cx + r_out + pad)
            y1 = min(image.shape[0], cy + r_out + pad)
            circles.append({
                'center': (cx, cy),
                'radius_outer': r_out,
                'radius_inner': r_in,
                'bbox': (x0, y0, x1, y1),
                'area': area,
                'circularity': circularity,
                'type': 'ring'
            })
            continue

        if len(cnt) >= 5:
            ellipse = cv2.fitEllipse(cnt)
            (ex, ey), (MA, ma), angle = ellipse
            ellipse_ratio = max(MA, ma) / min(MA, ma)
            if ellipse_ratio < max_ellipse_ratio:
                pad = int(max(MA, ma) * 0.2)
                x0 = max(0, int(ex - MA/2 - pad))
                y0 = max(0, int(ey - ma/2 - pad))
                x1 = min(image.shape[1], int(ex + MA/2 + pad))
                y1 = min(image.shape[0], int(ey + ma/2 + pad))
                circles.append({
                    'center': (int(ex), int(ey)),
                    'ellipse_axes': (int(MA/2), int(ma/2)),
                    'angle': angle,
                    'bbox': (x0, y0, x1, y1),
                    'area': area,
                    'circularity': circularity,
                    'ellipse_ratio': ellipse_ratio,
                    'type': 'ellipse'
                })
                continue

        (cx, cy), radius = cv2.minEnclosingCircle(cnt)
        cx, cy, radius = int(cx), int(cy), int(radius)
        pad = int(radius * 0.3)
        x0 = max(0, cx - radius - pad)
        y0 = max(0, cy - radius - pad)
        x1 = min(image.shape[1], cx + radius + pad)
        y1 = min(image.shape[0], cy + radius + pad)
        circles.append({
            'center': (cx, cy),
            'radius': radius,
            'bbox': (x0, y0, x1, y1),
            'area': area,
            'circularity': circularity,
            'type': 'circle'
        })
        print(f"Detected circle: Center=({cx},{cy}), Radius={radius}, Area={area}, Circularity={circularity:.3f}")
    return circles


def load_template(template_path):
    
    template = cv2.imread(template_path, cv2.IMREAD_COLOR)
    binary_template = preprocess_image(template)
    contours_template, _ = cv2.findContours(binary_template, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    template_contour = max(contours_template, key=cv2.contourArea)
    
    print(f"Template loaded: {template.shape}")
    return template_contour, template


def recognize_H(test_image, template_contour, threshold=0.5, min_area=100, max_area=None):
    
    binary = preprocess_image(test_image)
    
    
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    
    if max_area is None:
        max_area = test_image.shape[0] * test_image.shape[1] * 0.5
    
    candidates = []
    output_image = test_image.copy()
    
    for i, contour in enumerate(contours):
        area = cv2.contourArea(contour)
        
        if area < min_area or area > max_area:
            continue
        
        x, y, w, h = cv2.boundingRect(contour)
        margin = 30
        if x < margin or y < margin or (x + w) > (test_image.shape[1] - margin) or (y + h) > (test_image.shape[0] - margin):
            continue
        if w < 30 or h < 30:
            continue
        
        aspect_ratio = float(w) / h if h > 0 else 0
        if aspect_ratio < 0.4 or aspect_ratio > 1.8:
            continue
        
        sim1 = cv2.matchShapes(template_contour, contour, cv2.CONTOURS_MATCH_I1, 0)
        sim2 = cv2.matchShapes(template_contour, contour, cv2.CONTOURS_MATCH_I2, 0)
        
        
        similarity = min(sim1, sim2)
        
        if similarity < threshold and max(sim1, sim2) < threshold * 1.5:
            candidates.append({
                'bbox': (x, y, w, h),
                'similarity': similarity,
                'area': area,
                'aspect_ratio': aspect_ratio
            })
    
    candidates.sort(key=lambda x: x['similarity'])
    results = candidates[:1] if candidates else []
    
    for result in results:
        x, y, w, h = result['bbox']
        cv2.rectangle(output_image, (x, y), (x+w, y+h), (0, 255, 0), 2)
        cv2.putText(output_image, f"H ({result['similarity']:.3f})", 
                   (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    
    return results, output_image, binary


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
                from stream.wire_format import resolve_byte_order, sensor_frame_to_bgr

                camera = cam_manager.get_camera(camera_id, user_id)
                fmt = cam_manager.get_capture_format(camera_id)
                is_usb = isinstance(camera, dict) and camera.get("backend") == "cv2"
                byte_order = resolve_byte_order(fmt, libcamera_names=not is_usb)
                frame_bgr = sensor_frame_to_bgr(frame, byte_order)
                
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


def detection_thread(template_contour, threshold):
    global running

    frames_missed = 0
    n = 10  
    while running:
        try:
            if not frame_queue.empty():
                frame = frame_queue.get()
                circles = detect_circles(frame, min_circularity=0.65, min_area=8000)
                # print(f"Detected circles: {circles}")
                results = []
                output_image = frame.copy()
                found_H = False
                if circles:
                    circle = circles[0]
                    x0, y0, x1, y1 = circle['bbox']
                    crop = frame[y0:y1, x0:x1]
                    if crop.size > 0:
                        h_results, _, _ = recognize_H(
                            crop, template_contour, threshold,
                            min_area=2000, max_area=crop.shape[0] * crop.shape[1] * 0.5
                        )
                        print(f"Detected H in circle ROI: {h_results}")

                        cx, cy = circle['center']
                        if circle['type'] == 'circle':
                            radius = circle['radius']
                        elif circle['type'] == 'ring':
                            radius = circle['radius_outer']
                        elif circle['type'] == 'ellipse':
                            radius = max(circle['ellipse_axes'])
                        else:
                            radius = 0

                        cv2.circle(output_image, (cx, cy), int(radius), (255, 0, 255), 2)
                        cv2.circle(output_image, (cx, cy), 3, (255, 0, 255), -1)

                        for h_result in h_results:
                            hx, hy, hw, hh = h_result['bbox']
                            global_x = x0 + hx
                            global_y = y0 + hy

                            results.append({
                                'bbox': (global_x, global_y, hw, hh),
                                'similarity': h_result['similarity'],
                                'area': h_result['area'],
                                'circle_center': (cx, cy),
                                'circle_radius': radius
                            })

                            cv2.rectangle(output_image, (global_x, global_y),
                                          (global_x+hw, global_y+hh), (0, 255, 0), 3)
                        if h_results:
                            found_H = True
                            print("Found H within detected circle.")
                            frames_missed = 0
                        else:
                            frames_missed += 1
                    else:
                        frames_missed += 1
                else:
                    h_results, output_image, _ = recognize_H(frame, template_contour, threshold)
                    for h_result in h_results:
                        x, y, w, h = h_result['bbox']
                        results.append({
                            'bbox': (x, y, w, h),
                            'similarity': h_result['similarity'],
                            'area': h_result['area']
                        })
                        cv2.rectangle(output_image, (x, y), (x+w, y+h), (0, 255, 0), 3)
                    if h_results:
                        found_H = True
                        frames_missed = 0
                    else:
                        frames_missed += 1
                if frames_missed >= n and circles:
                    # print(f"Missed H for {frames_missed} frames, retrying all circles...")
                    for circle in circles:
                        x0, y0, x1, y1 = circle['bbox']
                        crop = frame[y0:y1, x0:x1]
                        if crop.size > 0:
                            h_results, _, _ = recognize_H(
                                crop, template_contour, threshold,
                                min_area=2000, max_area=crop.shape[0] * crop.shape[1] * 0.5
                            )
                            if h_results:
                                cx, cy = circle['center']
                                if circle['type'] == 'circle':
                                    radius = circle['radius']
                                elif circle['type'] == 'ring':
                                    radius = circle['radius_outer']
                                elif circle['type'] == 'ellipse':
                                    radius = max(circle['ellipse_axes'])
                                else:
                                    radius = 0

                                cv2.circle(output_image, (cx, cy), int(radius), (0, 255, 255), 2)
                                cv2.circle(output_image, (cx, cy), 3, (0, 255, 255), -1)

                                for h_result in h_results:
                                    hx, hy, hw, hh = h_result['bbox']
                                    global_x = x0 + hx
                                    global_y = y0 + hy
                                    results.append({
                                        'bbox': (global_x, global_y, hw, hh),
                                        'similarity': h_result['similarity'],
                                        'area': h_result['area'],
                                        'circle_center': (cx, cy),
                                        'circle_radius': radius
                                    })
                                    cv2.rectangle(output_image, (global_x, global_y),
                                                  (global_x+hw, global_y+hh), (0, 255, 255), 3)
                                found_H = True
                                frames_missed = 0
                                break  

                binary_image = preprocess_image(frame)

                if result_queue.full():
                    try:
                        result_queue.get_nowait()
                    except:
                        pass

                result_queue.put((results, output_image, binary_image, frame))
            else:
                time.sleep(0.001)
        except Exception as e:
            print(f"Detection error: {e}")
            time.sleep(0.01)


def main():
    
    global running
    
    template_path = "./templates/K.png"
    camera_id = 0
    user_id = "H_finder"
    threshold = 0.5
    
    try:
        template_contour, template_image = load_template(template_path)
        cam_manager = get_camera_manager()
        camera_config = {'format': 'RGB888', 'size': (640, 480)}
        
        camera = cam_manager.get_camera(camera_id, user_id, camera_config)
        
        capture_worker = Thread(target=capture_thread, args=(cam_manager, camera_id, user_id), daemon=True)
        detection_worker = Thread(target=detection_thread, args=(template_contour, threshold), daemon=True)
        
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