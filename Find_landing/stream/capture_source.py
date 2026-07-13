"""CaptureSource — sensor bytes; tách byte order (sensor) vs BGR (Hướng 2) vs wire (Hướng 1)."""

from __future__ import annotations

import time

from stream.wire_format import resolve_byte_order, sensor_frame_to_bgr, sensor_to_wire


class CaptureSource:
    """Picamera2/USB capture; chuẩn hóa sensor → BGR. Không encode, không CV."""

    def __init__(self, streamer, cam_manager):
        self.streamer = streamer
        self.cam_manager = cam_manager
        self.camera_id = streamer.config["camera_id"]
        self.user_id = "streamer"
        self.sensor_format = "RGB888"
        self.sensor_byte_order = "BGR888"

    def open(self) -> bool:
        streamer = self.streamer
        streamer._capture_ready.clear()
        streamer._capture_ok = False

        if self.cam_manager.is_camera_active(self.camera_id):
            print("️  Camera already in use, releasing...")
            self.cam_manager.release_camera(self.camera_id, self.user_id)
            time.sleep(1)

        camera_config = {
            "format": streamer.config["format"],
            "size": tuple(streamer.config["size"]),
            "device_path": streamer.config.get("camera_device", ""),
            "brightness": streamer.config.get("brightness", 0),
            "contrast": streamer.config.get("contrast", 1),
            "sharpness": streamer.config.get("sharpness", 1.5),
            "saturation": streamer.config.get("saturation", 1.0),
            "exposure_time": streamer.config.get("exposure_time", 0),
        }
        if streamer.config.get("libcamera_index") is not None:
            camera_config["libcamera_index"] = int(streamer.config["libcamera_index"])

        camera = self.cam_manager.get_camera(self.camera_id, self.user_id, camera_config)
        if camera is None:
            streamer._capture_ok = False
            streamer._capture_ready.set()
            return False

        self.sensor_format = self.cam_manager.get_capture_format(self.camera_id)
        is_usb = isinstance(camera, dict) and camera.get("backend") == "cv2"
        self.sensor_byte_order = resolve_byte_order(
            self.sensor_format, libcamera_names=not is_usb
        )
        streamer._capture_format = self.sensor_format
        streamer._sensor_byte_order = self.sensor_byte_order
        streamer._capture_ok = True
        streamer._capture_ready.set()
        return True

    def capture_raw(self):
        return self.cam_manager.capture_frame(self.camera_id, self.user_id)

    def capture_bgr(self):
        frame = self.capture_raw()
        if frame is None:
            return None
        return sensor_frame_to_bgr(frame, self.sensor_byte_order)

    def capture_wire(self, config: dict):
        """Stream-only: sensor → wire, không qua BGR (tránh lệch màu)."""
        frame = self.capture_raw()
        if frame is None:
            return None
        return sensor_to_wire(frame, self.sensor_byte_order, config)

    def drain(self) -> bool:
        """Bỏ frame cũ trên sensor khi encode_queue đầy — không qua Hướng 2."""
        return self.cam_manager.capture_frame(self.camera_id, self.user_id) is not None

    def close(self):
        self.cam_manager.release_camera(self.camera_id, self.user_id)
