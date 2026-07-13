"""EncoderSink — encode_queue + pipe writer (Hướng 1).

Nhận wire buffer (sensor_to_wire / bgr_to_wire); không biết sensor / OpenCV / FrameGate.
"""

import os
import queue
import time
from threading import Event, Thread
from typing import Optional


def np_as_tight_bytes(frame_wire) -> bytes:
    """Pack frame thành buffer tight rgb/bgr — không kèm row padding."""
    import numpy as np

    arr = np.ascontiguousarray(frame_wire)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"wire frame must be HxWx3, got {arr.shape}")
    return arr.tobytes()

# Writer thread riêng — ghi blocking đủ 1 frame; timeout ngắn + ghi dở gây lệch byte → 4 ô trên FFmpeg.
_PIPE_WRITE_TIMEOUT = 2.0
_ENCODE_QUEUE_SIZE = 1


class EncoderSink:
    """encode_queue maxsize=1 — drop khi đầy; writer thread đẩy pipe (không block capture)."""

    def __init__(self, pipe_write_fd: int, running: Event):
        self.pipe_write_fd = pipe_write_fd
        self.running = running
        self.frames_sent = 0
        self._queue: queue.Queue = queue.Queue(maxsize=_ENCODE_QUEUE_SIZE)
        self._writer: Optional[Thread] = None

    def start(self):
        self._writer = Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

    def stop(self):
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except queue.Empty:
                pass
        if self._writer:
            self._writer.join(timeout=3)

    def is_full(self) -> bool:
        return self._queue.full()

    def enqueue(self, frame_wire) -> int:
        """Enqueue wire-format frame; return số frame bị drop (0 hoặc 1)."""
        frame_bytes = np_as_tight_bytes(frame_wire)
        try:
            self._queue.put_nowait(frame_bytes)
            return 0
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame_bytes)
                return 1
            except queue.Empty:
                return 1

    def _write_frame_to_pipe(self, fd, frame_bytes, timeout=_PIPE_WRITE_TIMEOUT):
        """Ghi đủ 1 frame hoặc bỏ — không để byte dở trong pipe (FFmpeg rawvideo lệch)."""
        total = len(frame_bytes)
        offset = 0
        deadline = time.time() + timeout
        while offset < total and self.running.is_set():
            if time.time() >= deadline:
                return False
            try:
                written = os.write(fd, frame_bytes[offset:])
                if written == 0:
                    return False
                offset += written
            except BrokenPipeError:
                raise
            except OSError as e:
                if getattr(e, "errno", None) in (11, 35):  # EAGAIN / EWOULDBLOCK
                    time.sleep(0.002)
                    continue
                raise
        return offset >= total

    def _writer_loop(self):
        pipe_write_drops = 0
        while self.running.is_set():
            try:
                frame_bytes = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if frame_bytes is None:
                break
            try:
                if self._write_frame_to_pipe(self.pipe_write_fd, frame_bytes):
                    self.frames_sent += 1
                else:
                    pipe_write_drops += 1
            except BrokenPipeError:
                print("✗ Encoder pipe closed")
                self.running.clear()
                break
            except Exception as e:
                print(f"✗ Write error: {e}")
                self.running.clear()
                break
        if pipe_write_drops:
            print(f" [INFO] Pipe write drops (encoder slow): {pipe_write_drops}")
