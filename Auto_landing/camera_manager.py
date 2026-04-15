import threading
import time
from picamera2 import Picamera2
import numpy as np
import cv2
import glob
import re


class CameraManager:
  
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(CameraManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        with self._lock:
            if not self._initialized:
                self.cameras = {} 
                self.camera_locks = {}  
                self.camera_configs = {}  
                self.camera_users = {}  
                self._initialized = True
    
    def get_camera(self, camera_id, user_id=None, config=None):
      
        with self._lock:
            if camera_id not in self.cameras:
                try:
                    camera = self._initialize_camera(camera_id, config)
                    if camera is None:
                        return None
                    
                    self.cameras[camera_id] = camera
                    self.camera_locks[camera_id] = threading.Lock()
                    self.camera_configs[camera_id] = config or {}
                    self.camera_users[camera_id] = set()
                    
                    print(f"Camera {camera_id} đã được khởi tạo")
                except Exception as e:
                    print(f"Lỗi khởi tạo camera {camera_id}: {e}")
                    return None
            
            if user_id:
                self.camera_users[camera_id].add(user_id)
            
            return self.cameras[camera_id]
    
    def _initialize_camera(self, camera_id, config=None):

        try:
            # Check if camera exists first
            from picamera2 import Picamera2
            available_cameras = Picamera2.global_camera_info()
            
            if not available_cameras:
                print(f"✗ No cameras detected on system — trying USB/OpenCV fallback")
                return self._initialize_usb_fallback(camera_id, config)

            if camera_id >= len(available_cameras):
                print(f"✗ Camera {camera_id} not found. Available cameras: {len(available_cameras)}")
                print(f"   Available camera indices: 0-{len(available_cameras)-1}")
                # Try USB/OpenCV fallback when requested index is out of range
                return self._initialize_usb_fallback(camera_id, config)
            
            selected_info = available_cameras[camera_id] if camera_id < len(available_cameras) else {}

            camera = Picamera2(camera_id)
            
            
            default_config = {
                'format': 'RGB888',
                'size': (640, 480)
            }
            
            if config:
                default_config.update(config)
            
            main_cfg = {
                "format": default_config['format'],
                "size": default_config['size'],
            }

            # Streaming pipeline should prefer video configuration.
            # Some USB/libcamera paths are unstable with still configuration.
            try:
                camera_config = camera.create_video_configuration(main=main_cfg)
            except Exception:
                camera_config = camera.create_still_configuration(main=main_cfg)
            
            camera.configure(camera_config)
            camera.start()
            
            time.sleep(0.5)
            
            print(f"✓ Camera {camera_id} initialized successfully")
            return camera
            
        except Exception as e:
            print(f"✗ Error initializing camera {camera_id}: {e}")

            # Fallback path for USB cameras using V4L2/OpenCV.
            return self._initialize_usb_fallback(camera_id, config)

    def _sorted_video_nodes(self):
        nodes = glob.glob('/dev/video*')

        def sort_key(path):
            m = re.search(r'(\d+)$', path)
            return int(m.group(1)) if m else 9999

        return sorted(nodes, key=sort_key)

    def _initialize_usb_fallback(self, camera_id, config=None):
        device_path = None
        if config and isinstance(config, dict):
            device_path = config.get('device_path') or config.get('camera_device')

        candidates = []
        if device_path:
            candidates.append(device_path)

        nodes = self._sorted_video_nodes()
        high_nodes = [n for n in nodes if int(re.search(r'(\d+)$', n).group(1)) >= 8]
        low_nodes = [n for n in nodes if int(re.search(r'(\d+)$', n).group(1)) < 8]
        for node in high_nodes + low_nodes:
            if node not in candidates:
                candidates.append(node)

        width, height = (640, 480)
        if config and isinstance(config, dict):
            sz = config.get('size')
            if isinstance(sz, (tuple, list)) and len(sz) == 2:
                width, height = int(sz[0]), int(sz[1])

        for node in candidates:
            cap = cv2.VideoCapture(node, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap.release()
                continue

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                continue

            print(f"✓ USB fallback camera initialized on {node} (requested camera_id={camera_id})")
            return {
                'backend': 'cv2',
                'cap': cap,
                'device': node,
            }

        print(f"✗ USB fallback failed for camera {camera_id}")
        return None
    
    def capture_frame(self, camera_id, user_id=None):

        camera = self.get_camera(camera_id, user_id)
        if camera is None:
            return None
        
        lock = self.camera_locks.get(camera_id)
        if lock:
            with lock:
                try:
                    if isinstance(camera, dict) and camera.get('backend') == 'cv2':
                        cap = camera.get('cap')
                        if cap is None:
                            return None
                        ok, frame = cap.read()
                        if not ok or frame is None:
                            return None
                        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                    frame = camera.capture_array()
                    return frame
                except Exception as e:
                    print(f"Lỗi capture frame từ camera {camera_id}: {e}")
                    return None
        return None
    
    def release_camera(self, camera_id, user_id=None):
      
        with self._lock:
            if camera_id not in self.cameras:
                return
            
            if user_id and camera_id in self.camera_users:
                self.camera_users[camera_id].discard(user_id)
            
            if not self.camera_users.get(camera_id):
                try:
                    camera = self.cameras[camera_id]
                    if camera:
                        if isinstance(camera, dict) and camera.get('backend') == 'cv2':
                            cap = camera.get('cap')
                            if cap is not None:
                                cap.release()
                        else:
                            camera.stop()
                            camera.close()
                    
                    del self.cameras[camera_id]
                    del self.camera_locks[camera_id]
                    del self.camera_configs[camera_id]
                    del self.camera_users[camera_id]
                    
                    print(f"Camera {camera_id} đã được giải phóng")
                except Exception as e:
                    print(f"Lỗi giải phóng camera {camera_id}: {e}")
    
    def is_camera_active(self, camera_id):
      
        return camera_id in self.cameras
    
    def get_camera_users(self, camera_id):
      
        return self.camera_users.get(camera_id, set()).copy()
    
    def release_all_cameras(self):
       
        with self._lock:
            camera_ids = list(self.cameras.keys())
            for camera_id in camera_ids:
                try:
                    camera = self.cameras[camera_id]
                    if camera:
                        if isinstance(camera, dict) and camera.get('backend') == 'cv2':
                            cap = camera.get('cap')
                            if cap is not None:
                                cap.release()
                        else:
                            camera.stop()
                            camera.close()
                    print(f"Camera {camera_id} đã được dừng")
                except Exception as e:
                    print(f"Lỗi khi dừng camera {camera_id}: {e}")
            
            self.cameras.clear()
            self.camera_locks.clear()
            self.camera_configs.clear()
            self.camera_users.clear()



_camera_manager_instance = CameraManager()


def get_camera_manager():

    return _camera_manager_instance