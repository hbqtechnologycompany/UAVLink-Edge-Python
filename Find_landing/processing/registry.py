"""Đăng ký processor — chỉ nối pipeline, không chứa thuật toán."""

from typing import List

from processing.detect_config import frame_skip
from processing.detectors import create_processor
from processing.overlay import OverlayProcessor


def build_processor_list(
    config: dict,
    find_landing_dir: str = "",
    *,
    prepared=None,
    detection_on: bool,
    overlay_on: bool,
    overlay_processor: bool = True,
) -> List:
    processors = []
    if detection_on:
        processors.append(create_processor(config, find_landing_dir, prepared=prepared))
    if overlay_on and overlay_processor:
        processors.append(
            OverlayProcessor(
                overlay_enabled=True,
                detection_enabled=detection_on,
                frame_skip=frame_skip(config, "overlay_frame_skip", 5),
            )
        )
    return processors
