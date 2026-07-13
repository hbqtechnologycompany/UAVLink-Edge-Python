"""Types and processor interface for the image-processing pipeline."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np


@dataclass
class FrameMeta:
    frame_id: int
    output_size: tuple[int, int]


@dataclass
class ProcessResult:
    frame_id: int
    detected: bool = False
    overlay_frame: Optional[np.ndarray] = None
    telemetry: dict[str, Any] = field(default_factory=dict)


class FrameProcessor(ABC):
    """Plugin xử lý ảnh — chạy trên bản sao frame trong worker Hướng 2."""

    frame_skip: int = 1

    def wants_frame(self, frame_id: int) -> bool:
        """Hướng 1 chỉ feed worker khi processor cần frame này."""
        skip = max(int(self.frame_skip), 1)
        return frame_id % skip == 0

    @abstractmethod
    def process(self, frame_bgr: np.ndarray, meta: FrameMeta, state: dict) -> None:
        """Cập nhật state (detection, overlay_frame, telemetry)."""
