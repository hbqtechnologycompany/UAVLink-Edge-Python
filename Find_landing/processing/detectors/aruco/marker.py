import os

# Bảng marker chuẩn ArduPilot/PX4: DICT_4X4_50, ID 0–11 (3 cột × 4 hàng)
BOARD_COLS = 3
BOARD_ROWS = 4
BOARD_MARKER_COUNT = BOARD_COLS * BOARD_ROWS  # 12 marker: ID 0 … 11
BOARD_MARKER_IDS = tuple(range(BOARD_MARKER_COUNT))
BOARD_MARKER_PX = 200
BOARD_GAP_PX = 32
BOARD_LABEL_PX = 36
SINGLE_MARKER_PX = 400


def load_dictionary(name: str):
    import cv2

    key = str(name or "DICT_4X4_50").upper()
    dict_id = getattr(cv2.aruco, key, cv2.aruco.DICT_4X4_50)
    return cv2.aruco.getPredefinedDictionary(dict_id)


def marker_png_path(find_landing_dir: str, dictionary: str, marker_id: int) -> str:
    return os.path.join(
        find_landing_dir,
        "templates",
        f"aruco_{dictionary.lower()}_id{int(marker_id)}.png",
    )


def ensure_marker_png(find_landing_dir: str, dictionary: str, marker_id: int, *, force: bool = False) -> str:
    """Marker đơn ID 0–11 — in riêng từng tấm."""
    import cv2

    mid = int(marker_id)
    if mid < 0 or mid > BOARD_MARKER_COUNT - 1:
        raise ValueError(f"marker_id must be 0–{BOARD_MARKER_COUNT - 1}, got {mid}")

    os.makedirs(os.path.join(find_landing_dir, "templates"), exist_ok=True)
    path = marker_png_path(find_landing_dir, dictionary, mid)
    if not force and os.path.exists(path):
        return path

    img = cv2.aruco.generateImageMarker(load_dictionary(dictionary), mid, SINGLE_MARKER_PX)
    cv2.imwrite(path, img)
    print(f" [aruco v2] marker ID {mid}: {path}")
    return path


def ensure_all_marker_pngs(find_landing_dir: str, dictionary: str = "DICT_4X4_50", *, force: bool = False) -> list[str]:
    """Sinh đủ 12 file marker ID 0–11."""
    return [ensure_marker_png(find_landing_dir, dictionary, mid, force=force) for mid in BOARD_MARKER_IDS]


def ensure_board_sheet(find_landing_dir: str, dictionary: str = "DICT_4X4_50", *, force: bool = False) -> str:
    """
    Bảng in ArUco kiểu ArduPilot — 3×4, ID 0–11 (nhãn số dưới mỗi marker).
  Tham chiếu: https://ardupilot.org/dev/docs/ros-apriltag-detection.html
    """
    import cv2
    import numpy as np

    templates_dir = os.path.join(find_landing_dir, "templates")
    os.makedirs(templates_dir, exist_ok=True)
    fname = f"aruco_board_{dictionary.lower()}_0-11.png"
    path = os.path.join(templates_dir, fname)
    if not force and os.path.exists(path):
        return path

    dict_obj = load_dictionary(dictionary)
    cell = BOARD_MARKER_PX + BOARD_GAP_PX
    sheet_w = BOARD_COLS * cell + BOARD_GAP_PX
    sheet_h = BOARD_ROWS * (BOARD_MARKER_PX + BOARD_LABEL_PX) + BOARD_GAP_PX * (BOARD_ROWS + 1)
    sheet = np.full((sheet_h, sheet_w, 3), 255, dtype=np.uint8)

    for idx in BOARD_MARKER_IDS:
        row, col = divmod(idx, BOARD_COLS)
        marker = cv2.aruco.generateImageMarker(dict_obj, idx, BOARD_MARKER_PX)
        marker_bgr = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
        x0 = BOARD_GAP_PX + col * cell
        y0 = BOARD_GAP_PX + row * (BOARD_MARKER_PX + BOARD_LABEL_PX + BOARD_GAP_PX)
        sheet[y0 : y0 + BOARD_MARKER_PX, x0 : x0 + BOARD_MARKER_PX] = marker_bgr
        label = str(idx)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        label_x = x0 + (BOARD_MARKER_PX - tw) // 2
        label_y = y0 + BOARD_MARKER_PX + BOARD_LABEL_PX - 8
        cv2.putText(
            sheet,
            label,
            (label_x, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )

    cv2.imwrite(path, sheet)
    print(f" [aruco v2] board sheet 0–11: {path}")
    return path


def ensure_v2_templates(find_landing_dir: str, dictionary: str = "DICT_4X4_50", *, force: bool = False) -> dict:
    """Sinh đủ template v2: 12 marker đơn + 1 bảng ghép."""
    singles = ensure_all_marker_pngs(find_landing_dir, dictionary, force=force)
    board = ensure_board_sheet(find_landing_dir, dictionary, force=force)
    return {"board": board, "markers": singles}
