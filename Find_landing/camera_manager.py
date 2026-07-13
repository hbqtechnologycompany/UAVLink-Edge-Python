import threading
import time
from picamera2 import Picamera2
import numpy as np
import cv2
import glob
import re

from stream.wire_format import resolve_byte_order


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
            if camera_id in self.cameras and config:
                prev = self.camera_configs.get(camera_id) or {}
                prev_fmt = str(prev.get('format', '')).upper()
                new_fmt = str(config.get('format', '')).upper()
                prev_size = tuple(prev.get('size', ()))
                new_size = tuple(config.get('size', ()))
                if prev_fmt and new_fmt and prev_fmt != new_fmt:
                    print(f"Camera {camera_id}: format {prev_fmt} → {new_fmt}, reinit")
                    self._release_camera_unlocked(camera_id)
                elif prev_size and new_size and prev_size != new_size:
                    print(f"Camera {camera_id}: size {prev_size} → {new_size}, reinit")
                    self._release_camera_unlocked(camera_id)
                else:
                    merged = dict(prev)
                    merged.update(config or {})
                    self.camera_configs[camera_id] = merged
                    self._apply_image_controls(camera_id, merged)

            if camera_id not in self.cameras:
                try:
                    camera = self._initialize_camera(camera_id, config)
                    if camera is None:
                        return None
                    
                    self.cameras[camera_id] = camera
                    self.camera_locks[camera_id] = threading.Lock()
                    merged = dict(config or {})
                    if camera_id in self.camera_configs:
                        merged.update(self.camera_configs[camera_id])
                    self.camera_configs[camera_id] = merged
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

            lib_idx = int((config or {}).get('libcamera_index', camera_id))
            camera = Picamera2(lib_idx)
            
            
            default_config = {
                'format': 'RGB888',
                'size': (640, 480)
            }
            
            if config:
                default_config.update(config)

            picam_format = self._picam_format(default_config.get('format', 'RGB888'))
            target_w, target_h = int(default_config['size'][0]), int(default_config['size'][1])
            lib_idx = int(default_config.get('libcamera_index', camera_id))

            main_cfg = {
                "format": picam_format,
                "size": (target_w, target_h),
            }

            sensor_output = self._pick_sensor_output_size(camera, target_w, target_h)
            config_kwargs = {}
            if sensor_output:
                config_kwargs['sensor'] = {'output_size': sensor_output}
                print(f" Camera {camera_id}: sensor output {sensor_output[0]}x{sensor_output[1]} → main {target_w}x{target_h}")

            lores_cfg = None
            if default_config.get('processing_lores'):
                lw, lh = default_config.get('lores_size', (320, 240))
                lores_cfg = {"format": "RGB888", "size": (int(lw), int(lh))}
                print(f" Camera {camera_id}: lores {lw}x{lh} for CV")

            buf_count = int(default_config.get('buffer_count') or 3)
            if buf_count < 1:
                buf_count = 1
            if buf_count > 6:
                buf_count = 6

            # Streaming pipeline should prefer video configuration.
            # Some USB/libcamera paths are unstable with still configuration.
            try:
                if lores_cfg:
                    camera_config = camera.create_video_configuration(
                        main=main_cfg, lores=lores_cfg, buffer_count=buf_count, **config_kwargs
                    )
                else:
                    camera_config = camera.create_video_configuration(
                        main=main_cfg, buffer_count=buf_count, **config_kwargs
                    )
            except Exception:
                try:
                    if lores_cfg:
                        camera_config = camera.create_video_configuration(main=main_cfg, lores=lores_cfg)
                    else:
                        camera_config = camera.create_video_configuration(main=main_cfg)
                except Exception:
                    camera_config = camera.create_still_configuration(main=main_cfg)
            
            camera.configure(camera_config)
            camera.start()
            
            time.sleep(0.5)
            self._apply_image_controls(camera_id, default_config)

            actual_format = picam_format
            try:
                actual_format = camera.camera_configuration['main']['format']
            except Exception:
                pass
            if config is None:
                config = {}
            config = dict(config)
            config['actual_format'] = actual_format
            config['format'] = default_config.get('format', 'RGB888')
            config['has_lores'] = bool(lores_cfg)
            self.camera_configs[camera_id] = config
            
            print(f"✓ Camera {camera_id} initialized successfully (capture: {actual_format}, ui: {config['format']})")
            return camera
            
        except Exception as e:
            print(f"✗ Error initializing camera {camera_id}: {e}")

            # Fallback path for USB cameras using V4L2/OpenCV.
            return self._initialize_usb_fallback(camera_id, config)

    @staticmethod
    def _picam_format(ui_format):
        """Map UI wire format → Picamera2 libcamera main format.

        UI format = wire/encoder byte order (camera_config_*.json).
        Libcamera always uses RGB888 here: memory layout is B,G,R (OpenCV BGR).
        Color reorder for stream is done in stream/wire_format.py only.
        """
        fmt = str(ui_format or 'RGB888').upper()
        if fmt in ('BGR888', 'BGR'):
            # Picamera2 CSI streams are RGB-family; BGR is applied in software.
            return 'RGB888'
        if fmt in ('RGB888', 'RGB'):
            return 'RGB888'
        if fmt == 'YUV420':
            return 'YUV420'
        return 'RGB888'

    @staticmethod
    def _pick_sensor_output_size(camera, target_w: int, target_h: int):
        """Chọn sensor mode native gần nhất — giảm crop/scale lệch tỷ lệ."""
        try:
            modes = camera.sensor_modes
        except Exception:
            return None
        if not modes:
            return None

        target_ar = target_w / target_h if target_h else 4 / 3
        best_size = None
        best_score = float('inf')

        for mode in modes:
            sw, sh = mode.get('size', (0, 0))
            if sw <= 0 or sh <= 0:
                continue
            mode_ar = sw / sh
            ar_penalty = abs(mode_ar - target_ar) * 10000
            if sw < target_w or sh < target_h:
                size_penalty = (target_w - sw) ** 2 + (target_h - sh) ** 2
            else:
                size_penalty = (sw - target_w) * (sh - target_h) * 0.01
            score = ar_penalty + size_penalty
            if score < best_score:
                best_score = score
                best_size = (sw, sh)

        return best_size

    @staticmethod
    def _build_controls(config: dict) -> dict:
        """Map config UI → libcamera controls."""
        controls = {
            'Brightness': float(config.get('brightness', 0)),
            'Contrast': float(config.get('contrast', 1)),
            'Sharpness': float(config.get('sharpness', 1.5)),
            'Saturation': float(config.get('saturation', 1.0)),
        }
        exposure = int(config.get('exposure_time', 0) or 0)
        if exposure > 0:
            controls['AeEnable'] = False
            controls['ExposureTime'] = exposure
        else:
            controls['AeEnable'] = True
        return controls

    def _apply_image_controls(self, camera_id, config=None):
        camera = self.cameras.get(camera_id)
        if camera is None or isinstance(camera, dict):
            return
        cfg = dict(config or self.camera_configs.get(camera_id) or {})
        try:
            camera.set_controls(self._build_controls(cfg))
        except Exception as e:
            print(f"⚠ Camera {camera_id} controls: {e}")

    def get_capture_format(self, camera_id):
        cfg = self.camera_configs.get(camera_id) or {}
        return str(cfg.get('actual_format') or cfg.get('format') or 'RGB888')

    def get_sensor_byte_order(self, camera_id):
        fmt = self.get_capture_format(camera_id)
        cfg = self.camera_configs.get(camera_id) or {}
        is_usb = isinstance(self.cameras.get(camera_id), dict)
        return resolve_byte_order(fmt, libcamera_names=not is_usb)

    def get_lores_byte_order(self, camera_id):
        """Lores luôn Picamera2 RGB888 (libcamera) → byte thực B,G,R = BGR888 cho OpenCV."""
        cam = self.cameras.get(camera_id)
        if isinstance(cam, dict):
            return self.get_sensor_byte_order(camera_id)
        return resolve_byte_order("RGB888", libcamera_names=True)

    def has_lores(self, camera_id) -> bool:
        cfg = self.camera_configs.get(camera_id) or {}
        return bool(cfg.get('has_lores'))

    def capture_lores(self, camera_id, user_id=None):
        camera = self.get_camera(camera_id, user_id)
        if camera is None or isinstance(camera, dict):
            return None
        lock = self.camera_locks.get(camera_id)
        if not lock:
            return None
        with lock:
            try:
                return camera.capture_array("lores")
            except Exception as e:
                print(f"Lỗi capture lores camera {camera_id}: {e}")
                return None

    def _release_camera_unlocked(self, camera_id):
        if camera_id not in self.cameras:
            return
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
        except Exception as e:
            print(f"Lỗi giải phóng camera {camera_id}: {e}")
        for d in (self.cameras, self.camera_locks, self.camera_configs, self.camera_users):
            d.pop(camera_id, None)

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
            usb_cfg = dict(config or {})
            usb_cfg['actual_format'] = 'BGR888'
            self.camera_configs[camera_id] = usb_cfg
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
                        # OpenCV V4L2 returns BGR — keep native order.
                        return frame

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
                self._release_camera_unlocked(camera_id)
                print(f"Camera {camera_id} đã được giải phóng")
    
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