"""Làm mượt detection — chống nháy nhưng vẫn cập nhật realtime khi camera/marker di chuyển."""

from __future__ import annotations

import copy
from typing import Any


def _ema_scalar(prev: float | None, new: float, alpha: float) -> float:
    if prev is None:
        return float(new)
    return prev * (1.0 - alpha) + float(new) * alpha


def _ema_point(prev: tuple[float, float] | None, new: tuple[float, float], alpha: float) -> tuple[float, float]:
    if prev is None:
        return (float(new[0]), float(new[1]))
    return (
        _ema_scalar(prev[0], new[0], alpha),
        _ema_scalar(prev[1], new[1], alpha),
    )


def _ema_corners(
    prev: list[tuple[float, float]] | None,
    new: list[tuple[int, int]],
    alpha: float,
) -> list[tuple[float, float]]:
    if not new:
        return prev or []
    new_f = [(float(x), float(y)) for x, y in new]
    if not prev or len(prev) != len(new_f):
        return new_f
    return [_ema_point(p, n, alpha) for p, n in zip(prev, new_f)]


def _round_point(p: tuple[float, float]) -> tuple[int, int]:
    return int(round(p[0])), int(round(p[1]))


def _round_corners(corners: list[tuple[float, float]]) -> list[tuple[int, int]]:
    return [_round_point(p) for p in corners]


def _direction_hysteresis(
    offset_x: float,
    offset_y: float,
    prev: str,
    threshold: int = 20,
    hysteresis: int = 10,
) -> str:
    th_on = float(threshold)
    th_off = float(max(threshold - hysteresis, 6))
    prev = prev or "CENTER"
    parts: list[str] = []

    if "RIGHT" in prev:
        x_on = offset_x > th_off
    elif "LEFT" in prev:
        x_on = offset_x < -th_off
    else:
        x_on = abs(offset_x) > th_on
    if x_on:
        parts.append("RIGHT" if offset_x > 0 else "LEFT")

    if "DOWN" in prev:
        y_on = offset_y > th_off
    elif "UP" in prev:
        y_on = offset_y < -th_off
    else:
        y_on = abs(offset_y) > th_on
    if y_on:
        parts.append("DOWN" if offset_y > 0 else "UP")

    return " ".join(parts) if parts else "CENTER"


class SmoothTracker:
    """
    EMA nhẹ — giảm rung pixel, không đóng băng pose.
    Hold ngắn chỉ khi mất detection vài frame (chống nháy SEARCHING).
    """

    def __init__(
        self,
        *,
        ema_alpha: float = 0.28,
        hold_frames: int = 18,
        max_jump_frac: float = 0.14,
        direction_threshold: int = 20,
        direction_hysteresis: int = 15,
    ):
        self.ema_alpha = ema_alpha
        self.hold_frames = max(int(hold_frames), 1)
        self.max_jump_frac = max_jump_frac
        self.direction_threshold = direction_threshold
        self.direction_hysteresis = direction_hysteresis

        self._smooth: dict[str, Any] | None = None
        self._pos_f: tuple[float, float] | None = None
        self._size_f: tuple[float, float] | None = None
        self._corners_f: list[tuple[float, float]] | None = None
        self._markers_by_id_f: dict[int, list[tuple[float, float]]] = {}
        self._direction = "CENTER"
        self._miss_streak = 0

    def accept(self, raw: dict, output_size: tuple[int, int]) -> dict | None:
        out_w, out_h = output_size
        center_x, center_y = out_w // 2, out_h // 2

        if not raw.get("detected"):
            self._miss_streak += 1
            if self._smooth is not None and self._miss_streak <= self.hold_frames:
                held = copy.deepcopy(self._smooth)
                held["detected"] = True
                held["hold"] = True
                return held
            if self._miss_streak > self.hold_frames:
                self._reset()
            return {"detected": False}

        self._miss_streak = 0
        hx, hy = raw["h_position"]
        new_pos = (float(hx), float(hy))

        max_dim = max(out_w, out_h, 1)
        max_jump = max_dim * self.max_jump_frac
        if self._pos_f is not None:
            dx = new_pos[0] - self._pos_f[0]
            dy = new_pos[1] - self._pos_f[1]
            if (dx * dx + dy * dy) ** 0.5 > max_jump:
                new_pos = (
                    self._pos_f[0] + max(-max_jump, min(dx, max_jump)),
                    self._pos_f[1] + max(-max_jump, min(dy, max_jump)),
                )

        alpha = self.ema_alpha
        self._pos_f = _ema_point(self._pos_f, new_pos, alpha)

        bw, bh = raw.get("h_size", (0, 0))
        self._size_f = _ema_point(self._size_f, (float(bw), float(bh)), alpha)

        raw_corners = raw.get("aruco_corners") or []
        self._corners_f = _ema_corners(self._corners_f, raw_corners, alpha)

        raw_by_id = raw.get("aruco_markers_by_id") or {}
        for mid, corners in raw_by_id.items():
            key = int(mid)
            prev = self._markers_by_id_f.get(key)
            self._markers_by_id_f[key] = _ema_corners(prev, corners, alpha)

        sx, sy = _round_point(self._pos_f)
        sw, sh = _round_point(self._size_f) if self._size_f else (int(bw), int(bh))
        off_x = int(round(sx - center_x))
        off_y = int(round(center_y - sy))
        self._direction = _direction_hysteresis(
            float(off_x),
            float(off_y),
            self._direction,
            self.direction_threshold,
            self.direction_hysteresis,
        )

        result = copy.deepcopy(raw)
        result["detected"] = True
        result["h_position"] = (sx, sy)
        result["h_size"] = (max(1, sw), max(1, sh))
        result["offset_x"] = off_x
        result["offset_y"] = off_y
        result["direction"] = self._direction
        result["smoothed"] = True
        if self._corners_f:
            result["aruco_corners"] = _round_corners(self._corners_f)
        if self._markers_by_id_f:
            by_id = {mid: _round_corners(c) for mid, c in self._markers_by_id_f.items()}
            result["aruco_markers_by_id"] = by_id
            result["aruco_markers"] = list(by_id.values())
            result["aruco_visible_ids"] = sorted(by_id.keys())

        self._smooth = result
        return result

    def _reset(self) -> None:
        self._smooth = None
        self._pos_f = None
        self._size_f = None
        self._corners_f = None
        self._markers_by_id_f = {}
        self._direction = "CENTER"
        self._miss_streak = 0
