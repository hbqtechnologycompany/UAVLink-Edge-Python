"""ArUco v2 — bảng marker kiểu ArduPilot (DICT_4X4_50, ID 0–11)."""

from .marker import BOARD_MARKER_COUNT

BOARD_ID_MAX = BOARD_MARKER_COUNT - 1


def get_direction(offset_x: int, offset_y: int, threshold: int = 20) -> str:
    direction = ""
    if abs(offset_x) > threshold:
        direction += "RIGHT " if offset_x > 0 else "LEFT "
    if abs(offset_y) > threshold:
        direction += "DOWN " if offset_y > 0 else "UP "
    return direction.strip() or "CENTER"


def _board_markers(corners, ids):
    out = []
    for i, mid in enumerate(ids.flatten()):
        mid = int(mid)
        if mid < 0 or mid > BOARD_ID_MAX:
            continue
        pts = corners[i].reshape(4, 2)
        cx = float(pts[:, 0].mean())
        cy = float(pts[:, 1].mean())
        out.append({"id": mid, "center": (cx, cy), "corners": pts})
    return out


def _pick_landing(board_markers: list, marker_id: int):
    """Chỉ dùng đúng marker_id đã chọn (0–11)."""
    if not board_markers:
        return None

    target = int(marker_id)
    if target < 0 or target > BOARD_ID_MAX:
        return None

    chosen = next((m for m in board_markers if m["id"] == target), None)
    if chosen is None:
        return None

    return {
        "id": chosen["id"],
        "center": chosen["center"],
        "corners": chosen["corners"],
        "board_mode": len(board_markers) > 1,
        "marker_count": len(board_markers),
        "visible_ids": [m["id"] for m in board_markers],
    }


def detect_frame(
    frame_bgr,
    output_size: tuple[int, int],
    detector,
    *,
    marker_id: int = 0,
    detect_size: tuple[int, int] | None = None,
) -> dict:
    import cv2

    det_w, det_h = detect_size or (frame_bgr.shape[1], frame_bgr.shape[0])
    if (frame_bgr.shape[1], frame_bgr.shape[0]) != (det_w, det_h):
        small = cv2.resize(frame_bgr, (det_w, det_h), interpolation=cv2.INTER_AREA)
    else:
        small = frame_bgr

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None or len(ids) == 0:
        return {"detected": False}

    board_markers = _board_markers(corners, ids)
    landing = _pick_landing(board_markers, marker_id)
    if landing is None:
        return {"detected": False}

    cx, cy = landing["center"]
    pts = landing["corners"]
    w = float(pts[:, 0].max() - pts[:, 0].min())
    h = float(pts[:, 1].max() - pts[:, 1].min())

    w_out, h_out = output_size
    sx = w_out / det_w
    sy = h_out / det_h
    h_x = int(round(cx * sx))
    h_y = int(round(cy * sy))
    box_w = max(8, int(round(w * sx)))
    box_h = max(8, int(round(h * sy)))
    center_x, center_y = w_out // 2, h_out // 2

    all_marker_corners = []
    for m in board_markers if board_markers else []:
        scaled = [
            (int(round(p[0] * sx)), int(round(p[1] * sy)))
            for p in m["corners"].reshape(4, 2)
        ]
        all_marker_corners.append(scaled)

    primary_corners = [
        (int(round(p[0] * sx)), int(round(p[1] * sy))) for p in pts.reshape(4, 2)
    ]

    markers_by_id: dict[int, list[tuple[int, int]]] = {}
    for m in board_markers if board_markers else []:
        scaled = [
            (int(round(p[0] * sx)), int(round(p[1] * sy)))
            for p in m["corners"].reshape(4, 2)
        ]
        markers_by_id[int(m["id"])] = scaled

    return {
        "detected": True,
        "detector": "aruco",
        "mode": "board" if landing.get("board_mode") else "aruco",
        "version": "v2",
        "has_marker": True,
        "h_position": (h_x, h_y),
        "h_size": (box_w, box_h),
        "offset_x": h_x - center_x,
        "offset_y": center_y - h_y,
        "similarity": 0.99,
        "direction": get_direction(h_x - center_x, center_y - h_y),
        "aruco_id": landing["id"],
        "aruco_corners": primary_corners,
        "aruco_markers": all_marker_corners,
        "aruco_markers_by_id": markers_by_id,
        "aruco_visible_ids": landing.get("visible_ids", []),
        "aruco_marker_count": landing.get("marker_count", 1),
        "in_circle": False,
    }
