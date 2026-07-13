"""FrameGate — Hướng 2 → Hướng 1: không bao giờ block stream.

gate_timeout_ms=0 (mặc định): lấy overlay/detection mới nhất, không chờ.
gate_timeout_ms>0: chỉ gắn overlay khi Hướng 2 đã xử lý xong frame trong cửa sổ skew;
  nếu chưa kịp → bỏ qua frame đó (giữ FPS).
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from processing.pipeline import ProcessingPipeline


class FrameGate:
    """Capture → (optional latest overlay) → encode; Hướng 2 không stall Hướng 1."""

    def __init__(self, config: dict, pipeline: Optional[ProcessingPipeline]):
        self.config = config
        self.pipeline = pipeline
        ms = int(config.get("gate_timeout_ms", 0))
        self.timeout_sec = max(ms, 0) / 1000.0

    def resolve(self, frame_id: int, frame_bgr: np.ndarray) -> np.ndarray:
        if self.pipeline is None or not self.pipeline.enabled:
            return frame_bgr

        if self.timeout_sec > 0:
            fps = max(int(self.config.get("framerate", 30)), 1)
            skew = max(1, int(round(self.timeout_sec * fps)))
            result = self.pipeline.result_for_stream(frame_id, max_frame_skew=skew)
            if result is None:
                return frame_bgr
            if result.overlay_frame is not None:
                return result.overlay_frame
            return frame_bgr

        return self.pipeline.apply_for_stream(frame_id, frame_bgr)
