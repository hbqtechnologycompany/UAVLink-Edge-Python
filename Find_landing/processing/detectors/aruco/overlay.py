"""Overlay ArUco v2 — vẽ toàn bộ marker trên bảng + tâm đáp."""

from processing.overlay_style import (
    FONT_SCALE_LABEL,
    MARKER_BORDER,
    MARKER_BORDER_HI,
    TARGET_DOT_R,
    put_text_line,
)


def draw(frame, detection_result: dict):
    import cv2
    import numpy as np

    by_id = detection_result.get("aruco_markers_by_id")
    if by_id:
        for corners in by_id.values():
            if len(corners) >= 4:
                pts = np.array(corners, dtype=np.int32).reshape((-1, 1, 2))
                cv2.polylines(frame, [pts], True, (0, 200, 0), MARKER_BORDER)
    else:
        for marker_corners in detection_result.get("aruco_markers") or []:
            if len(marker_corners) >= 4:
                pts = np.array(marker_corners, dtype=np.int32).reshape((-1, 1, 2))
                cv2.polylines(frame, [pts], True, (0, 200, 0), MARKER_BORDER)

    corners = detection_result.get("aruco_corners")
    if corners:
        pts = np.array(corners, dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(frame, [pts], True, (0, 255, 0), MARKER_BORDER_HI)

    aid = detection_result.get("aruco_id", 0)
    n = detection_result.get("aruco_marker_count", 1)
    ids = detection_result.get("aruco_visible_ids", [])
    label = f"ARUCO v2 ID={aid}"
    if n > 1:
        label += f" ({n} visible: {ids})"

    put_text_line(frame, 0, label, (0, 255, 0), FONT_SCALE_LABEL)
    h_x, h_y = detection_result["h_position"]
    cv2.circle(frame, (h_x, h_y), TARGET_DOT_R, (0, 0, 255), -1, lineType=cv2.LINE_AA)
    return frame
