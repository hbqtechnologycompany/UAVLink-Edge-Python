"""Đọc tham số CV chung từ camera_config — không chứa thuật toán detection."""


def detect_size_from_config(config: dict | None) -> tuple[int, int]:
    max_w, max_h = 320, 240
    if not config:
        return max_w, max_h
    lores = config.get("lores_size")
    if isinstance(lores, (list, tuple)) and len(lores) >= 2:
        w, h = int(lores[0]), int(lores[1])
        if w > 0 and h > 0:
            return w, h
    size = config.get("size")
    if isinstance(size, (list, tuple)) and len(size) >= 2:
        w, h = int(size[0]), int(size[1])
        if w > 0 and h > 0 and w * h <= max_w * max_h:
            return w, h
    return max_w, max_h


def frame_skip(config: dict, key: str = "detect_frame_skip", default: int = 3) -> int:
    try:
        return max(int(config.get(key, default)), 1)
    except (TypeError, ValueError):
        return default
