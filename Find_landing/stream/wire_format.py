"""Wire format — tách rõ Hướng 2 (xử lý) vs Hướng 1 (stream).

Hướng 2 (processing): luôn làm việc trên BGR (OpenCV). sensor_frame_to_bgr().
Hướng 1 (stream wire): buffer khớp config format + ffmpeg/gst rawvideo.
  - stream-only: sensor_to_wire() — sensor → wire, không qua pipeline CV.
  - có processing: bgr_to_wire() — sau FrameGate, BGR → wire.
  - H264+CV: apply_sensor_to_ui_wire() trong pre_callback trước HW encode.

Picamera2/libcamera: tên format đảo so với byte thực (RGB888 = [B,G,R] trong RAM).
USB/V4L2 (OpenCV): BGR888 = [B,G,R] đúng nghĩa.
"""

from __future__ import annotations


def normalize_ui_format(fmt: str) -> str:
    """Wire / encoder channel order theo config UI."""
    f = str(fmt or "RGB888").upper()
    return "BGR888" if f in ("BGR888", "BGR") else "RGB888"


def resolve_byte_order(reported_format: str, *, libcamera_names: bool = True) -> str:
    """Byte order thực trong buffer capture_array / cap.read()."""
    f = str(reported_format or "RGB888").upper()
    if libcamera_names:
        # libcamera DRM: RGB888 → memory B,G,R; BGR888 → memory R,G,B
        if f in ("RGB888", "RGB", "XRGB8888", "RGBA8888"):
            return "BGR888"
        if f in ("BGR888", "BGR", "XBGR8888", "BGRA8888"):
            return "RGB888"
    else:
        if f in ("BGR888", "BGR"):
            return "BGR888"
        if f in ("RGB888", "RGB"):
            return "RGB888"
    return "BGR888"


def _reorder_channels(frame, src_order: str, dst_order: str):
    import cv2

    if src_order == dst_order:
        return frame
    if src_order == "BGR888" and dst_order == "RGB888":
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    if src_order == "RGB888" and dst_order == "BGR888":
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return frame


def _resize_interp(src_w: int, src_h: int, dst_w: int, dst_h: int):
    import cv2

    if dst_w < src_w or dst_h < src_h:
        return cv2.INTER_AREA
    if dst_w > src_w * 1.5 or dst_h > src_h * 1.5:
        return cv2.INTER_CUBIC
    return cv2.INTER_LINEAR


def _center_crop_to_aspect(frame, target_ar: float):
    """Crop giữa khung hình để khớp tỷ lệ đích — tránh kéo giãn méo ảnh."""
    import numpy as np

    h, w = frame.shape[:2]
    src_ar = w / h if h else target_ar
    if abs(src_ar - target_ar) < 0.02:
        return frame
    if src_ar > target_ar:
        new_w = max(1, int(round(h * target_ar)))
        x0 = max(0, (w - new_w) // 2)
        return frame[:, x0 : x0 + new_w]
    new_h = max(1, int(round(w / target_ar)))
    y0 = max(0, (h - new_h) // 2)
    return frame[y0 : y0 + new_h, :]


def _prepare_frame(frame, config: dict):
    import cv2
    import numpy as np

    out = np.asarray(frame)
    if out.ndim == 3 and out.shape[2] > 3:
        out = out[:, :, :3]
    size = config.get("size") or [640, 480]
    target_w, target_h = int(size[0]), int(size[1])
    h, w = out.shape[:2]
    if (w, h) == (target_w, target_h):
        return out
    target_ar = target_w / target_h if target_h else 4 / 3
    out = _center_crop_to_aspect(out, target_ar)
    h, w = out.shape[:2]
    if (w, h) != (target_w, target_h):
        interp = _resize_interp(w, h, target_w, target_h)
        out = cv2.resize(out, (target_w, target_h), interpolation=interp)
    return out


def sensor_frame_to_bgr(frame, byte_order: str):
    """Sensor buffer → BGR cho Hướng 2 (OpenCV / detection / overlay)."""
    import numpy as np

    if byte_order == "BGR888":
        return np.array(frame, copy=True)
    return _reorder_channels(frame, byte_order, "BGR888")


def sensor_to_wire(frame, byte_order: str, config: dict):
    """Stream-only: sensor bytes → wire buffer (không qua Hướng 2)."""
    import numpy as np

    out = _prepare_frame(frame, config)
    wire_order = normalize_ui_format(config.get("format", "RGB888"))
    out = _reorder_channels(out, byte_order, wire_order)
    return np.ascontiguousarray(out)


def bgr_to_wire(frame_bgr, config: dict):
    """Sau FrameGate: BGR (Hướng 2) → wire buffer cho encoder."""
    import numpy as np

    out = _prepare_frame(frame_bgr, config)
    wire_order = normalize_ui_format(config.get("format", "RGB888"))
    out = _reorder_channels(out, "BGR888", wire_order)
    return np.ascontiguousarray(out)


def wire_pixel_format(config: dict) -> str:
    """GStreamer rawvideoparse / ffmpeg -pix_fmt — khớp wire buffer."""
    return "bgr" if normalize_ui_format(config.get("format", "RGB888")) == "BGR888" else "rgb"


def apply_sensor_to_ui_wire(frame, config: dict, sensor_byte_order: str) -> None:
    """Đặt buffer main theo Stream format UI — in-place, trước H264 HW encode."""
    import cv2

    wire_order = normalize_ui_format(config.get("format", "RGB888"))
    if sensor_byte_order == wire_order:
        return
    if frame is None or getattr(frame, "ndim", 0) != 3 or frame.shape[2] < 3:
        return
    if sensor_byte_order == "BGR888" and wire_order == "RGB888":
        cv2.cvtColor(frame, cv2.COLOR_BGR2RGB, dst=frame)
    elif sensor_byte_order == "RGB888" and wire_order == "BGR888":
        cv2.cvtColor(frame, cv2.COLOR_RGB2BGR, dst=frame)
