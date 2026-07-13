"""Khối chờ xử lý ảnh — Hướng 2.

Thêm phương án detection: tạo folder processing/detectors/<tên>/ và đăng ký trong
processing/detectors/__init__.py (MODE_ALIASES).
"""

import queue
from threading import Event, Lock, Thread
from typing import Optional

import numpy as np

from processing.base import FrameMeta, ProcessResult
from processing.detectors import prepare, resolve_mode
from processing.registry import build_processor_list


def build_pipeline(config: dict, find_landing_dir: str, running: Event, *, overlay_processor: bool = True):
    """Tạo pipeline xử lý theo config; None khi light mode / CV tắt hết."""
    detection_on = config.get("detection_enabled", True)
    overlay_on = config.get("overlay_enabled", True)
    if not detection_on and not overlay_on:
        return None

    prepared = None
    mode = resolve_mode(config)
    if detection_on:
        try:
            prepared = prepare(config, find_landing_dir)
            print(f" Landing detection mode: {mode}")
        except Exception as e:
            print(f" Detection init failed ({mode}): {e}")
            detection_on = False

    processors = build_processor_list(
        config,
        find_landing_dir,
        prepared=prepared,
        detection_on=detection_on,
        overlay_on=overlay_on,
        overlay_processor=overlay_processor,
    )
    if not processors:
        return None

    return ProcessingPipeline(processors, config, running)


class ProcessingPipeline:
    """process_queue maxsize=1 — worker plugin chain; Hướng 1 đọc latest, không block."""

    def __init__(self, processors: list, config: dict, running: Event):
        self.processors = processors
        self.config = config
        self.running = running
        self.enabled = True
        self._overlay_enabled = bool(config.get("overlay_enabled", True))
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._lock = Lock()
        self._latest: Optional[ProcessResult] = None
        self._latest_detection: dict = {"detected": False}
        self._detections_count = 0
        self._display_miss = 0
        self._display_hold = max(int(config.get("overlay_hold_frames", 18)), 4)
        self._worker: Optional[Thread] = None

    @property
    def detections_count(self) -> int:
        return self._detections_count

    def latest_telemetry(self) -> dict:
        with self._lock:
            if self._latest and self._latest.telemetry:
                return dict(self._latest.telemetry)
            det = self._latest_detection
            return {
                "offset_x": det.get("offset_x"),
                "offset_y": det.get("offset_y"),
                "direction": det.get("direction"),
                "similarity": det.get("similarity"),
            }

    def latest_detection(self) -> dict:
        with self._lock:
            return dict(self._latest_detection)

    def result_for_stream(self, frame_id: int, max_frame_skew: int = 0):
        """Hướng 2 → Hướng 1: trả kết quả nếu khớp frame (hoặc trong skew); không chờ."""
        with self._lock:
            latest = self._latest
            if latest is None:
                return None
            if latest.frame_id == frame_id:
                return latest
            if max_frame_skew > 0 and abs(latest.frame_id - frame_id) <= max_frame_skew:
                return latest
        return None

    def wants_feed(self, frame_id: int) -> bool:
        return any(p.wants_frame(frame_id) for p in self.processors)

    def start(self):
        self._worker = Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def stop(self):
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except queue.Empty:
                pass
        if self._worker:
            self._worker.join(timeout=2)

    def submit(self, frame_id: int, frame_bgr: np.ndarray):
        job = (frame_id, np.array(frame_bgr, copy=True))
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(job)
            except queue.Empty:
                pass

    def apply_for_stream(self, frame_id: int, frame_bgr: np.ndarray) -> np.ndarray:
        if self.wants_feed(frame_id):
            self.submit(frame_id, frame_bgr)
        if not self._overlay_enabled:
            return frame_bgr
        with self._lock:
            latest = self._latest
        if latest is not None and latest.overlay_frame is not None:
            return latest.overlay_frame
        return frame_bgr

    def wait_and_resolve(self, frame_id: int, frame_bgr: np.ndarray, timeout_sec: float) -> np.ndarray:
        import time

        self.submit(frame_id, frame_bgr)
        deadline = time.time() + timeout_sec
        while self.running.is_set() and time.time() < deadline:
            with self._lock:
                if self._latest is not None and self._latest.frame_id == frame_id:
                    if self._latest.overlay_frame is not None:
                        return self._latest.overlay_frame
                    break
            time.sleep(0.001)
        return frame_bgr

    def _worker_loop(self):
        w_out, h_out = self.config["size"]
        output_size = (w_out, h_out)
        state: dict = {"detection_result": {"detected": False}}

        while self.running.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break

            frame_id, frame_bgr = job
            state["overlay_frame"] = None
            meta = FrameMeta(frame_id=frame_id, output_size=output_size)

            for proc in self.processors:
                proc.process(frame_bgr, meta, state)

            prev_count = self._detections_count
            self._detections_count = state.get("detections_count", prev_count)

            detection = state.get("detection_result") or {"detected": False}
            result = ProcessResult(
                frame_id=frame_id,
                detected=bool(detection.get("detected")),
                overlay_frame=state.get("overlay_frame"),
                telemetry={
                    "offset_x": detection.get("offset_x"),
                    "offset_y": detection.get("offset_y"),
                    "direction": detection.get("direction"),
                    "similarity": detection.get("similarity"),
                },
            )

            with self._lock:
                self._latest = result
                if detection.get("detected"):
                    self._latest_detection = dict(detection)
                    self._display_miss = 0
                elif self._latest_detection.get("detected") and self._display_miss < self._display_hold:
                    self._display_miss += 1
                else:
                    self._latest_detection = dict(detection)
                    self._display_miss += 1
