"""Thuật toán nhận H — grayscale + contour, không dùng màu RGB."""

_REF_FRAME_AREA = 640 * 480


def get_direction(offset_x: int, offset_y: int, threshold: int = 20) -> str:
    direction = ""
    if abs(offset_x) > threshold:
        direction += "RIGHT " if offset_x > 0 else "LEFT "
    if abs(offset_y) > threshold:
        direction += "DOWN " if offset_y > 0 else "UP "
    return direction.strip() or "CENTER"


def _scaled_min_pad_area(detect_size: tuple[int, int]) -> int:
    frame_area = detect_size[0] * detect_size[1]
    return max(1500, int(8000 * frame_area / _REF_FRAME_AREA))


def _circle_radius(circle: dict) -> float:
    ctype = circle.get("type", "circle")
    if ctype == "ring":
        return float(circle.get("radius_outer", 0))
    if ctype == "ellipse":
        axes = circle.get("ellipse_axes", (0, 0))
        return float(max(axes) if axes else 0)
    return float(circle.get("radius", 0))


def _result_from_match(match: dict, output_size: tuple[int, int], sx: float, sy: float) -> dict:
    x, y, w, h = match["bbox"]
    h_x = int((x + w // 2) * sx)
    h_y = int((y + h // 2) * sy)
    w_out = int(w * sx)
    h_out = int(h * sy)
    center_x, center_y = output_size[0] // 2, output_size[1] // 2
    offset_x = h_x - center_x
    offset_y = center_y - h_y

    circle_center = None
    circle_radius = None
    in_circle = False
    if "circle_center" in match:
        cx, cy = match["circle_center"]
        circle_center = (int(cx * sx), int(cy * sy))
        circle_radius = int(match["circle_radius"] * max(sx, sy))
        dist = ((h_x - circle_center[0]) ** 2 + (h_y - circle_center[1]) ** 2) ** 0.5
        in_circle = dist <= circle_radius

    return {
        "detected": True,
        "detector": "contour_h",
        "mode": "pad+h" if in_circle else "h",
        "version": "v1",
        "has_marker": True,
        "h_position": (h_x, h_y),
        "h_size": (w_out, h_out),
        "offset_x": offset_x,
        "offset_y": offset_y,
        "similarity": float(match.get("similarity", 0)),
        "in_circle": in_circle,
        "circle_center": circle_center,
        "circle_radius": circle_radius,
        "direction": get_direction(offset_x, offset_y),
    }


def detect_frame(
    small_bgr,
    template_contour,
    output_size: tuple[int, int],
    threshold: float = 0.8,
    *,
    detect_size: tuple[int, int] | None = None,
    allow_fullframe_fallback: bool = True,
) -> dict:
    import find

    if template_contour is None:
        return {"detected": False}

    det_w, det_h = detect_size or (small_bgr.shape[1], small_bgr.shape[0])
    w_out, h_out = output_size
    sx = w_out / det_w
    sy = h_out / det_h
    frame_area = small_bgr.shape[0] * small_bgr.shape[1]

    circles = find.detect_circles(
        small_bgr,
        min_circularity=0.65,
        min_area=_scaled_min_pad_area((det_w, det_h)),
    )

    if circles:
        for circle in circles:
            x0, y0, x1, y1 = circle["bbox"]
            crop = small_bgr[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            h_results, _, _ = find.recognize_H(
                crop,
                template_contour,
                threshold,
                min_area=max(200, int(2000 * crop.shape[0] * crop.shape[1] / frame_area)),
                max_area=int(crop.shape[0] * crop.shape[1] * 0.5),
            )
            if h_results:
                best = h_results[0]
                cx, cy = circle["center"]
                return _result_from_match(
                    {
                        "bbox": (
                            x0 + best["bbox"][0],
                            y0 + best["bbox"][1],
                            best["bbox"][2],
                            best["bbox"][3],
                        ),
                        "similarity": best["similarity"],
                        "circle_center": (cx, cy),
                        "circle_radius": _circle_radius(circle),
                    },
                    output_size,
                    sx,
                    sy,
                )

    if allow_fullframe_fallback:
        h_results, _, _ = find.recognize_H(small_bgr, template_contour, threshold)
        if h_results:
            best = h_results[0]
            return _result_from_match(
                {"bbox": best["bbox"], "similarity": best["similarity"]},
                output_size,
                sx,
                sy,
            )

    return {"detected": False}
