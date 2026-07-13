from processing.base import FrameMeta, FrameProcessor

from .detect import detect_frame
from .stability import StableTracker


class ContourHProcessor(FrameProcessor):
    def __init__(
        self,
        template_contour,
        enabled: bool = True,
        frame_skip: int = 3,
        threshold: float = 0.8,
        allow_fullframe_fallback: bool = True,
        detect_size: tuple[int, int] | None = None,
    ):
        self.template_contour = template_contour
        self.enabled = enabled
        self.frame_skip = max(int(frame_skip), 1)
        self.threshold = float(threshold)
        self.allow_fullframe_fallback = bool(allow_fullframe_fallback)
        self.detect_size = detect_size or (320, 240)
        self._stable = StableTracker()

    def process(self, frame_bgr, meta: FrameMeta, state: dict) -> None:
        if not self.enabled or not self.wants_frame(meta.frame_id):
            return

        import cv2

        det_w, det_h = self.detect_size
        fh, fw = frame_bgr.shape[:2]
        if (fw, fh) != (det_w, det_h):
            small_bgr = cv2.resize(frame_bgr, (det_w, det_h), interpolation=cv2.INTER_AREA)
        else:
            small_bgr = frame_bgr

        try:
            raw = detect_frame(
                small_bgr,
                self.template_contour,
                meta.output_size,
                threshold=self.threshold,
                detect_size=(det_w, det_h),
                allow_fullframe_fallback=self.allow_fullframe_fallback,
            )
            stable = self._stable.accept(raw, meta.output_size)
            if stable and stable.get("detected"):
                state["detection_result"] = stable
                state["detections_count"] = state.get("detections_count", 0) + 1
            else:
                state["detection_result"] = stable if stable is not None else {"detected": False}
        except Exception as e:
            print(f" [contour_h] detection error: {e}")
            state["detection_result"] = {"detected": False}
