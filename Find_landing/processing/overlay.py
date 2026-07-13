"""Overlay chung stream — crosshair, offset, telemetry. Graphics theo từng detector."""

import numpy as np

from processing.detectors import draw_detection_overlay
from processing.overlay_style import (
    CROSSHAIR_ARM,
    FONT_SCALE_BODY,
    FONT_SCALE_LABEL,
    GUIDE_LINE,
    LINE_THIN,
    put_text_line,
)


def scale_detection_to_frame(detection_result: dict, ref_size: tuple[int, int], frame_size: tuple[int, int]) -> dict:
    if not detection_result or not detection_result.get("detected"):
        return detection_result

    ref_w, ref_h = ref_size
    fw, fh = frame_size
    if ref_w <= 0 or ref_h <= 0 or (ref_w, ref_h) == (fw, fh):
        return detection_result

    sx = fw / ref_w
    sy = fh / ref_h
    out = dict(detection_result)
    hx, hy = detection_result["h_position"]
    hw, hh = detection_result["h_size"]
    out["h_position"] = (int(round(hx * sx)), int(round(hy * sy)))
    out["h_size"] = (max(1, int(round(hw * sx))), max(1, int(round(hh * sy))))
    out["offset_x"] = int(round(detection_result.get("offset_x", 0) * sx))
    out["offset_y"] = int(round(detection_result.get("offset_y", 0) * sy))
    if detection_result.get("circle_center"):
        cx, cy = detection_result["circle_center"]
        out["circle_center"] = (int(round(cx * sx)), int(round(cy * sy)))
    if detection_result.get("circle_radius"):
        out["circle_radius"] = max(1, int(round(detection_result["circle_radius"] * max(sx, sy))))
    if detection_result.get("aruco_corners"):
        out["aruco_corners"] = [
            (int(round(x * sx)), int(round(y * sy))) for x, y in detection_result["aruco_corners"]
        ]
    return out


def draw_overlay(frame, detection_result, overlay_enabled: bool = True, *, coord_ref=None):
    import cv2

    if not overlay_enabled or not detection_result:
        return frame

    if not detection_result.get("detected", False):
        put_text_line(frame, 0, "SEARCHING...", (0, 255, 255), FONT_SCALE_LABEL)
        return frame

    fh, fw = frame.shape[:2]
    if coord_ref is not None:
        detection_result = scale_detection_to_frame(detection_result, coord_ref, (fw, fh))

    cx, cy = fw // 2, fh // 2
    cv2.line(frame, (cx - CROSSHAIR_ARM, cy), (cx + CROSSHAIR_ARM, cy), (255, 0, 0), LINE_THIN, lineType=cv2.LINE_AA)
    cv2.line(frame, (cx, cy - CROSSHAIR_ARM), (cx, cy + CROSSHAIR_ARM), (255, 0, 0), LINE_THIN, lineType=cv2.LINE_AA)

    draw_detection_overlay(frame, detection_result)

    h_x, h_y = detection_result["h_position"]
    cv2.line(frame, (h_x, h_y), (cx, cy), (0, 255, 255), GUIDE_LINE, lineType=cv2.LINE_AA)

    offset_x = detection_result["offset_x"]
    offset_y = detection_result["offset_y"]
    direction = detection_result["direction"]
    # Dòng 0: label ArUco (trong draw_detection_overlay); telemetry từ dòng 1 — góc phải trên
    put_text_line(
        frame, 1, f"Offset: X={offset_x:+4d} Y={offset_y:+4d}", (255, 255, 255), FONT_SCALE_BODY,
    )
    if direction != "CENTER":
        put_text_line(frame, 2, f"Move: {direction}", (0, 255, 255), FONT_SCALE_BODY)
    else:
        put_text_line(frame, 2, "ALIGNED!", (0, 255, 0), FONT_SCALE_BODY)
    sim = detection_result.get("similarity", 0)
    put_text_line(frame, 3, f"Score: {sim:.3f}", (255, 255, 255), FONT_SCALE_BODY)
    return frame


from processing.base import FrameProcessor


class OverlayProcessor(FrameProcessor):
    def __init__(self, overlay_enabled: bool = True, detection_enabled: bool = True, frame_skip: int = 5):
        self.overlay_enabled = overlay_enabled
        self.detection_enabled = detection_enabled
        self.frame_skip = max(int(frame_skip), 1)

    def process(self, frame_bgr, meta, state: dict) -> None:
        if not self.overlay_enabled or not self.wants_frame(meta.frame_id):
            return

        detection = state.get("detection_result")
        out = np.array(frame_bgr, copy=True)
        if detection and detection.get("detected"):
            state["overlay_frame"] = draw_overlay(out, detection, True)
        elif self.detection_enabled:
            put_text_line(out, 0, "SEARCHING...", (0, 255, 255), FONT_SCALE_LABEL)
            state["overlay_frame"] = out
