"""Hướng 1 — capture, FrameGate, wire format, encode.

Avoid eager imports here — camera_manager imports stream.wire_format and
must not pull capture_loop (circular import via camera_manager).
"""

__all__ = [
    "run_capture_loop",
    "CaptureSource",
    "EncoderSink",
    "FrameGate",
    "bgr_to_wire",
    "wire_pixel_format",
]


def __getattr__(name):
    if name == "run_capture_loop":
        from stream.capture_loop import run_capture_loop
        return run_capture_loop
    if name == "CaptureSource":
        from stream.capture_source import CaptureSource
        return CaptureSource
    if name == "EncoderSink":
        from stream.encoder import EncoderSink
        return EncoderSink
    if name == "FrameGate":
        from stream.frame_gate import FrameGate
        return FrameGate
    if name == "bgr_to_wire":
        from stream.wire_format import bgr_to_wire
        return bgr_to_wire
    if name == "wire_pixel_format":
        from stream.wire_format import wire_pixel_format
        return wire_pixel_format
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
