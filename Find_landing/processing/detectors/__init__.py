"""Registry phương án landing detection — mỗi phương án = một folder.

Thêm phương án: tạo folder mới + đăng ký MODE_ALIASES.
Xóa phương án: xóa folder + xóa dòng alias tương ứng ở đây.
"""

from __future__ import annotations

import importlib
from typing import Any

# alias config → tên folder
MODE_ALIASES = {
    "contour": "contour_h",
    "contour_h": "contour_h",
    "h": "contour_h",
    "aruco": "aruco",
    "fiducial": "aruco",
    "marker_aruco": "aruco",
    "v2": "aruco",
    "v1": "contour_h",
}


def resolve_mode(config: dict) -> str:
    raw = str(config.get("landing_detection_mode", "contour_h") or "contour_h").strip().lower()
    return MODE_ALIASES.get(raw, "contour_h")


def load_plugin(mode: str):
    return importlib.import_module(f"processing.detectors.{mode}")


def list_modes() -> list[str]:
    seen = set()
    out = []
    for folder in sorted(MODE_ALIASES.values()):
        if folder not in seen:
            seen.add(folder)
            out.append(folder)
    return out


def prepare(config: dict, find_landing_dir: str) -> Any:
    plugin = load_plugin(resolve_mode(config))
    if getattr(plugin, "needs_template", lambda: False)():
        return plugin.prepare(find_landing_dir)
    return None


def create_processor(config: dict, find_landing_dir: str, prepared: Any = None):
    plugin = load_plugin(resolve_mode(config))
    return plugin.create_processor(config, find_landing_dir, prepared=prepared)


def draw_detection_overlay(frame, detection_result: dict):
    if not detection_result or not detection_result.get("detected"):
        return frame
    detector = detection_result.get("detector") or resolve_mode({})
    plugin = load_plugin(detector)
    return plugin.draw_overlay(frame, detection_result)
