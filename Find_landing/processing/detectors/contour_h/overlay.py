"""Overlay riêng phương án contour_h — xóa folder này thì không còn vẽ H/vòng."""

from processing.overlay_style import (
    BOX_BORDER,
    CROSSHAIR_HALF,
    FONT_SCALE_LABEL,
    GUIDE_LINE,
    INNER_DOT_R,
    TARGET_DOT_R,
    put_text_line,
)


def draw(frame, detection_result: dict):
    import cv2

    h_x, h_y = detection_result["h_position"]
    w, h = detection_result["h_size"]

    cv2.rectangle(
        frame,
        (h_x - w // 2, h_y - h // 2),
        (h_x + w // 2, h_y + h // 2),
        (0, 255, 0),
        BOX_BORDER,
        lineType=cv2.LINE_AA,
    )
    cv2.line(
        frame, (h_x - CROSSHAIR_HALF, h_y), (h_x + CROSSHAIR_HALF, h_y),
        (0, 0, 255), GUIDE_LINE, lineType=cv2.LINE_AA,
    )
    cv2.line(
        frame, (h_x, h_y - CROSSHAIR_HALF), (h_x, h_y + CROSSHAIR_HALF),
        (0, 0, 255), GUIDE_LINE, lineType=cv2.LINE_AA,
    )
    cv2.circle(frame, (h_x, h_y), TARGET_DOT_R, (0, 0, 255), -1, lineType=cv2.LINE_AA)

    if detection_result.get("in_circle") and detection_result.get("circle_center"):
        cx, cy = detection_result["circle_center"]
        r = detection_result["circle_radius"]
        cv2.circle(frame, (cx, cy), r, (255, 0, 255), 2, lineType=cv2.LINE_AA)
        cv2.circle(frame, (cx, cy), INNER_DOT_R, (255, 0, 255), -1, lineType=cv2.LINE_AA)
        put_text_line(frame, 0, "LANDING AREA", (255, 0, 255), FONT_SCALE_LABEL)
    return frame
