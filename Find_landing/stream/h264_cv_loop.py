"""Hướng 1 thống nhất: Picamera2 HW H264 main → FFmpeg copy → RTSP.

Hướng 2 (Video processing) tùy chọn: lores async, đồng bộ overlay nếu kịp gate — không chờ, không tuột FPS.
Cả cam0 và cam1 luôn đi cùng pipeline Hướng 1; bật/tắt CV không đổi đường stream.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from threading import Event, Lock, Thread

from camera_manager import get_camera_manager
from processing.pipeline import build_pipeline
from processing.overlay import draw_overlay
from stream.metrics import landing_path, write_landing_telemetry, write_stats
from stream.wire_format import apply_sensor_to_ui_wire, normalize_ui_format, sensor_frame_to_bgr


def _cv_enabled(config: dict) -> bool:
    return bool(config.get("detection_enabled") or config.get("overlay_enabled"))


def _lores_size(config: dict) -> tuple[int, int]:
    custom = config.get("lores_size")
    if isinstance(custom, (list, tuple)) and len(custom) >= 2:
        return int(custom[0]), int(custom[1])
    return 320, 240


def _overlay_skip(config: dict) -> int:
    try:
        return max(int(config.get("overlay_frame_skip", 5)), 1)
    except (TypeError, ValueError):
        return 5


def _overlay_burn_enabled(config: dict) -> bool:
    if not config.get("overlay_enabled"):
        return False
    return bool(config.get("overlay_burn_enabled", True))


def _buffer_count(config: dict) -> int:
    try:
        n = int(config.get("buffer_count") or 3)
    except (TypeError, ValueError):
        n = 3
    return max(1, min(n, 6))


def _gate_frame_skew(config: dict, fps: int) -> int:
    """Số frame Hướng 2 được phép lệch so với frame stream khi gate_timeout_ms > 0."""
    try:
        gate_ms = int(config.get("gate_timeout_ms", 0))
    except (TypeError, ValueError):
        gate_ms = 0
    if gate_ms <= 0:
        return 0
    return max(1, int(round(gate_ms * max(fps, 1) / 1000.0)))


def _open_camera_with_retry(cam_manager, camera_id, user_id, cam_cfg, attempts: int = 5):
    camera = None
    for attempt in range(attempts):
        if cam_manager.is_camera_active(camera_id):
            cam_manager.release_camera(camera_id, user_id)
            time.sleep(0.5)
        camera = cam_manager.get_camera(camera_id, user_id, cam_cfg)
        if camera is not None and not isinstance(camera, dict):
            return camera
        if attempt + 1 < attempts:
            print(f" Camera {camera_id} busy — retry {attempt + 2}/{attempts}...")
            time.sleep(1.5)
    return None


def _read_external_landing(camera_id: int) -> dict:
    try:
        with open(landing_path(camera_id), "r") as f:
            data = json.load(f)
        if time.time() - float(data.get("updated_at", 0)) > 2.0:
            return {"detected": False}
        return {
            "detected": bool(data.get("detected")),
            "offset_x": data.get("offset_x"),
            "offset_y": data.get("offset_y"),
            "direction": data.get("direction"),
            "similarity": data.get("similarity"),
        }
    except Exception:
        return {"detected": False}


def _adaptive_overlay_skip(base_skip: int, window_fps: float, memory_tier: int) -> int:
    skip = max(base_skip, 1)
    if window_fps < 22:
        skip = max(skip, base_skip + 2)
    if window_fps < 18:
        skip = max(skip, base_skip + 4)
    if memory_tier <= 2 and window_fps < 25:
        skip = max(skip, 8)
    return skip


def _apply_overlay_to_main(buf, config: dict, detection: dict, ref_w: int, ref_h: int) -> None:
    draw_overlay(
        buf,
        detection,
        overlay_enabled=True,
        coord_ref=(ref_w, ref_h),
    )


def run_h264_stream_loop(streamer) -> bool:
    """
    Hướng 1 (luôn): HW H264 encode + FFmpeg RTSP.
    Hướng 2 (tùy chọn): Video processing trên lores — không block Hướng 1.
    """
    from picamera2 import MappedArray
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FileOutput

    config = streamer.config
    find_landing_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cam_manager = get_camera_manager()
    camera_id = config["camera_id"]
    user_id = "streamer_h264"
    cv_on = _cv_enabled(config)
    external_landing = bool(config.get("external_landing")) or os.environ.get("DRONEBRIDGE_EXTERNAL_LANDING") == "1"
    memory_tier = int(config.get("memory_tier_gb") or 4)

    if cam_manager.is_camera_active(camera_id):
        cam_manager.release_camera(camera_id, user_id)
        time.sleep(0.5)

    streamer._capture_ready.clear()
    streamer._capture_ok = False

    cam_cfg = {
        "format": config.get("format", "RGB888"),
        "size": tuple(config["size"]),
        "processing_lores": cv_on and not external_landing,
        "buffer_count": _buffer_count(config),
        "brightness": config.get("brightness", 0),
        "contrast": config.get("contrast", 1),
        "sharpness": config.get("sharpness", 1.5),
        "saturation": config.get("saturation", 1.0),
        "exposure_time": config.get("exposure_time", 0),
    }
    if cam_cfg["processing_lores"]:
        cam_cfg["lores_size"] = _lores_size(config)
    if config.get("libcamera_index") is not None:
        cam_cfg["libcamera_index"] = int(config["libcamera_index"])

    camera = _open_camera_with_retry(cam_manager, camera_id, user_id, cam_cfg)
    if camera is None:
        streamer._capture_ok = False
        streamer._capture_ready.set()
        return False

    if cv_on and not external_landing and not cam_manager.has_lores(camera_id):
        cam_manager.release_camera(camera_id, user_id)
        streamer._capture_ok = False
        streamer._capture_ready.set()
        return False

    byte_order = cam_manager.get_lores_byte_order(camera_id) if cam_manager.has_lores(camera_id) else None
    main_byte_order = cam_manager.get_sensor_byte_order(camera_id)
    streamer._capture_ok = True
    streamer._capture_ready.set()

    processing = None
    if cv_on and not external_landing:
        processing = build_pipeline(
            config,
            find_landing_dir,
            streamer.running,
            overlay_processor=False,
        )
        if processing:
            processing.start()
            print(" Hướng 2: Video processing worker (lores, async — không block stream)")

    overlay_on = bool(config.get("overlay_enabled"))
    overlay_burn = _overlay_burn_enabled(config)
    frame_counter = {"id": 0}
    encode_counter = {"n": 0}
    encode_drops = {"n": 0}
    slow_callbacks = {"n": 0}
    det_cache = {"data": {"detected": False}}
    det_lock = Lock()
    burn_hold_frames = max(int(config.get("overlay_hold_frames", 18)), 4)
    burn_lock = {"det": {"detected": False}, "miss": 0}

    fps = int(config.get("framerate", 30))
    bitrate_kbps = int(config.get("bitrate", 5000))
    gop = int(config.get("keyframe_interval", 30))
    ref_w, ref_h = int(config["size"][0]), int(config["size"][1])

    try:
        streamer.gst_process = subprocess.Popen(
            streamer._ffmpeg_h264_copy_rtsp_cmd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        print(f"✗ FFmpeg H264 copy start failed: {e}")
        cam_manager.release_camera(camera_id, user_id)
        return False

    encoder = H264Encoder(
        bitrate=bitrate_kbps * 1000,
        repeat=True,
        iperiod=gop,
        framerate=fps,
    )
    output = FileOutput(streamer.gst_process.stdin)

    wire = normalize_ui_format(config.get("format", "RGB888"))
    needs_wire_swap = main_byte_order != wire

    def _current_detection() -> dict:
        if external_landing:
            return _read_external_landing(camera_id)
        if processing:
            return processing.latest_detection()
        with det_lock:
            return dict(det_cache["data"])

    def _resolve_burn_detection() -> dict:
        fresh = _current_detection()
        if fresh.get("detected"):
            burn_lock["miss"] = 0
            burn_lock["det"] = dict(fresh)
            return burn_lock["det"]
        if burn_lock["det"].get("detected") and burn_lock["miss"] < burn_hold_frames:
            burn_lock["miss"] += 1
            return burn_lock["det"]
        burn_lock["miss"] += 1
        if burn_lock["miss"] >= burn_hold_frames:
            burn_lock["det"] = {"detected": False}
        return burn_lock["det"]

    def _try_burn_overlay(buf, fid: int) -> None:
        if not overlay_on or not overlay_burn:
            return
        det = _resolve_burn_detection()
        if not det.get("detected"):
            return
        _apply_overlay_to_main(buf, config, det, ref_w, ref_h)

    def pre_callback(request):
        fid = frame_counter["id"]
        t0 = time.perf_counter()
        try:
            with MappedArray(request, "main") as mapped:
                buf = mapped.array
                if needs_wire_swap:
                    apply_sensor_to_ui_wire(buf, config, main_byte_order)
                _try_burn_overlay(buf, fid)
        except Exception as exc:
            if fid % 150 == 0:
                print(f" pre_callback skipped: {exc}")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if elapsed_ms > (1000.0 / max(fps, 1)) * 0.6:
            slow_callbacks["n"] += 1
            if slow_callbacks["n"] % 30 == 1:
                encode_drops["n"] += 1
        frame_counter["id"] = fid + 1
        encode_counter["n"] += 1

    camera.pre_callback = pre_callback

    try:
        camera.start_encoder(encoder, output, name="main")
    except Exception as e:
        print(f"✗ Picamera2 H264 encoder failed: {e}")
        streamer.stop()
        cam_manager.release_camera(camera_id, user_id)
        return False

    h2_note = "OFF"
    if external_landing:
        h2_note = "external landing file"
    elif processing:
        h2_note = f"lores {_lores_size(config)}"
    swap_note = "on" if needs_wire_swap else "off"
    role = config.get("stream_role", "primary")
    print(
        f" Hướng 1 (stream): main {config['size']} @ {fps}fps | HW H264 → FFmpeg → RTSP | "
        f"role={role} | wire={wire} swap={swap_note} | buffer={_buffer_count(config)}"
    )
    print(
        f" Hướng 2 (CV): {h2_note} | burn realtime + hold ngắn {burn_hold_frames}f khi mất det | "
        f"RAM {memory_tier}GB"
    )

    cv_stop = Event()

    def lores_worker():
        if external_landing or not processing:
            return
        fid = 0
        interval = 1.0 / max(fps, 1)
        last_tick = 0.0
        while streamer.running.is_set() and not cv_stop.is_set():
            now = time.time()
            if now - last_tick < interval:
                time.sleep(0.001)
                continue
            last_tick = now
            if processing.wants_feed(fid):
                frame = cam_manager.capture_lores(camera_id, user_id)
                if frame is not None and byte_order:
                    bgr = sensor_frame_to_bgr(frame, byte_order)
                    processing.submit(fid, bgr)
                    with det_lock:
                        det_cache["data"] = processing.latest_detection()
            fid += 1

    cv_thread = Thread(target=lores_worker, daemon=True)
    cv_thread.start()

    streamer.start_time = time.time()
    last_stats = time.time()
    last_sent_stats = 0
    last_landing_write = 0.0
    low_fps_streak = 0

    try:
        while streamer.running.is_set():
            if streamer.gst_process and streamer.gst_process.poll() is not None:
                print("✗ FFmpeg H264 copy stopped unexpectedly")
                streamer.running.clear()
                break

            now = time.time()
            streamer.frames_sent = encode_counter["n"]
            if external_landing:
                det = _read_external_landing(camera_id)
                streamer.detection_result = det
            elif processing:
                streamer.detections_count = processing.detections_count
                streamer.detection_result = processing.latest_detection()
                if now - last_landing_write >= 0.1:
                    write_landing_telemetry(camera_id, streamer.detection_result, processing.detections_count)
                    last_landing_write = now

            if now - last_stats >= 5.0:
                elapsed = now - streamer.start_time
                window_fps = (encode_counter["n"] - last_sent_stats) / 5.0
                avg_fps = encode_counter["n"] / elapsed if elapsed > 0 else 0
                det_count = processing.detections_count if processing else 0
                drop_note = ""
                if encode_drops["n"] or slow_callbacks["n"]:
                    drop_note = f" | drops={encode_drops['n']} slow_cb={slow_callbacks['n']}"
                print(
                    f" Stats: {encode_counter['n']} encoded @ {avg_fps:.1f} fps | "
                    f"window {window_fps:.1f} fps | H2 det={det_count}{drop_note}"
                )
                if window_fps < 18:
                    low_fps_streak += 1
                else:
                    low_fps_streak = 0
                write_stats(config, encode_counter["n"], streamer.start_time, avg_fps, encode_drops["n"], window_fps)
                last_sent_stats = encode_counter["n"]
                last_stats = now

            time.sleep(0.05)
    finally:
        cv_stop.set()
        cv_thread.join(timeout=2)
        try:
            camera.stop_encoder()
        except Exception:
            pass
        try:
            camera.pre_callback = None
        except Exception:
            pass
        cam_manager.release_camera(camera_id, user_id)
        if processing:
            processing.stop()
        print(" Camera released (Hướng 1 stream)")

    return True


# Alias tương thích
run_h264_cv_loop = run_h264_stream_loop
