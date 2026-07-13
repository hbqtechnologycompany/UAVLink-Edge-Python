"""v1: H contour + vòng tròn bãi đáp (template H.png, logic gốc find.py)."""

MODE_ID = "contour_h"


def needs_template() -> bool:
    return True


def prepare(find_landing_dir: str):
    from .template import load_template

    return load_template(find_landing_dir)


def create_processor(config: dict, find_landing_dir: str, prepared=None):
    from processing.detect_config import detect_size_from_config, frame_skip

    from .processor import ContourHProcessor

    template_contour, _ = prepared if prepared else (None, None)
    return ContourHProcessor(
        template_contour,
        enabled=True,
        frame_skip=frame_skip(config),
        threshold=float(config.get("detection_threshold", 0.8)),
        allow_fullframe_fallback=bool(config.get("landing_fullframe_fallback", True)),
        detect_size=detect_size_from_config(config),
    )


def draw_overlay(frame, detection_result: dict):
    from .overlay import draw

    return draw(frame, detection_result)
