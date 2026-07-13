"""Overlay cố định pixel (1:1) — không scale theo resolution stream."""

from __future__ import annotations

import cv2

FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE_LABEL = 0.48
FONT_SCALE_BODY = 0.44
FONT_THICKNESS = 2

LINE_THIN = 2
CROSSHAIR_ARM = 18
GUIDE_LINE = 2
BOX_BORDER = 2
MARKER_BORDER = 2
MARKER_BORDER_HI = 2
TARGET_DOT_R = 5
CROSSHAIR_HALF = 12
INNER_DOT_R = 2

# Góc phải trên — tránh sidebar / panel trái của QCC viewer
TEXT_CORNER = "top_right"
TOP_MARGIN = 18
RIGHT_MARGIN = 14
LEFT_MARGIN = 64
LINE_HEIGHT = 22


def text_xy(frame, line_index: int, text: str, scale: float, corner: str | None = None) -> tuple[int, int]:
    """Vị trí putText cố định pixel; line_index xếp chồng theo chiều dọc."""
    _fh, fw = frame.shape[:2]
    corner = corner or TEXT_CORNER
    (tw, th), _baseline = cv2.getTextSize(text, FONT, scale, FONT_THICKNESS)
    y = TOP_MARGIN + line_index * LINE_HEIGHT + th
    if corner == "top_right":
        x = max(RIGHT_MARGIN, fw - RIGHT_MARGIN - tw)
    else:
        x = LEFT_MARGIN
    return int(x), int(y)


def put_text_line(frame, line_index: int, text: str, color, scale: float | None = None, corner: str | None = None) -> None:
    scale = FONT_SCALE_BODY if scale is None else scale
    x, y = text_xy(frame, line_index, text, scale, corner)
    cv2.putText(
        frame,
        text,
        (x, y),
        FONT,
        scale,
        color,
        FONT_THICKNESS,
        cv2.LINE_AA,
    )
