"""v2: ArUco board — chuẩn ArduPilot / PX4 / ROS (bảng marker DICT_4X4_50)."""

MODE_ID = "aruco"


def needs_template() -> bool:
    return False


def prepare(find_landing_dir: str):
    return None


def create_processor(config: dict, find_landing_dir: str, prepared=None):
    from processing.detect_config import detect_size_from_config, frame_skip

    from .processor import ArucoProcessor

    return ArucoProcessor(
        find_landing_dir,
        enabled=True,
        frame_skip=frame_skip(config),
        marker_id=int(config.get("aruco_marker_id", 0) or 0),
        dictionary=str(config.get("aruco_dictionary", "DICT_4X4_50")),
        detect_size=detect_size_from_config(config),
    )


def draw_overlay(frame, detection_result: dict):
    from .overlay import draw

    return draw(frame, detection_result)
