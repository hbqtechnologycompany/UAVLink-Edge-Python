from processing.base import FrameMeta, FrameProcessor

from .compat import create_aruco_detector
from .detect import detect_frame
from .marker import ensure_v2_templates, load_dictionary
from .stability import StableTracker


class ArucoProcessor(FrameProcessor):
    def __init__(
        self,
        find_landing_dir: str,
        enabled: bool = True,
        frame_skip: int = 3,
        marker_id: int = 0,
        dictionary: str = "DICT_4X4_50",
        detect_size: tuple[int, int] | None = None,
    ):
        self.enabled = enabled
        self.frame_skip = max(int(frame_skip), 1)
        self.marker_id = int(marker_id)
        if self.marker_id < 0 or self.marker_id > 11:
            raise ValueError(f"aruco_marker_id must be 0–11, got {self.marker_id}")
        self.dictionary = str(dictionary or "DICT_4X4_50").upper()
        self.detect_size = detect_size or (320, 240)
        self._stable = StableTracker()

        self._detector = create_aruco_detector(load_dictionary(self.dictionary))
        templates = ensure_v2_templates(find_landing_dir, self.dictionary)
        print(
            f" [aruco v2] track ID={self.marker_id} only | "
            f"board: {templates['board']} | markers 0–11: {len(templates['markers'])} files"
        )

    def process(self, frame_bgr, meta: FrameMeta, state: dict) -> None:
        if not self.enabled or not self.wants_frame(meta.frame_id):
            return
        try:
            raw = detect_frame(
                frame_bgr,
                meta.output_size,
                self._detector,
                marker_id=self.marker_id,
                detect_size=self.detect_size,
            )
            stable = self._stable.accept(raw, meta.output_size)
            if stable and stable.get("detected"):
                state["detection_result"] = stable
                state["detections_count"] = state.get("detections_count", 0) + 1
            else:
                state["detection_result"] = stable if stable is not None else {"detected": False}
        except Exception as e:
            print(f" [aruco v2] detection error: {e}")
            state["detection_result"] = {"detected": False}
