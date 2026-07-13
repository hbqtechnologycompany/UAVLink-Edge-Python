# -*- coding: utf-8 -*-
"""
Camera Streamer — CSI/USB capture → H.264 → MediaMTX (RTSP push).

Latency (honest):
  - HW passthrough (detection+overlay OFF): libcamera H.264 → GStreamer → RTSP.
    Typical glass-to-glass ~80–200 ms + network (WebRTC viewer adds more).
  - Python CV path (detection/overlay ON): Picamera2 → OpenCV → x264enc → RTSP.
    Typical ~200–500 ms+; NOT suitable for sub-ms requirements.

Sub-millisecond streaming requires dedicated HW encode + minimal buffers end-to-end;
this module targets practical low-latency drone telemetry video (WebRTC/H.264).
"""

import subprocess
import json
import os
import sys
import time
import platform
from threading import Thread, Event
import signal
import shutil

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from stream.wire_format import wire_pixel_format

_active_streamer = None
_shutdown_requested = False

# Required keys in camera_config_*.json (generated from config.yaml by DroneBridge).
_REQUIRED_CONFIG_KEYS = (
    'publish_path', 'mediamtx_host', 'mediamtx_port', 'drone_id',
)


class CameraStreamer:
    def __init__(self, config_path='camera_config_0.json'):
        """Initialize camera streamer with configuration from DroneBridge."""
        if os.path.isabs(config_path):
            self.config_path = config_path
        else:
            self.config_path = os.path.join(os.path.dirname(__file__), config_path)
        self.config = self.load_config()
        self.running = Event()
        self.detection_result = None
        self.gst_process = None
        self.rpicam_process = None
        self.gst_launch_path = self._find_gst_launch()
        self.pipe_read_fd = None
        
        self.frames_sent = 0
        self.detections_count = 0
        self.start_time = None
        
        self.template_contour = None
        self.template_image = None
        self._capture_ready = Event()
        self._capture_ok = False
    def load_config(self):
        """Load configuration written by DroneBridge (config.yaml → camera_config_*.json)."""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(
                f"Missing {self.config_path} — start camera from DroneBridge or save config in web UI"
            )
        with open(self.config_path, 'r') as f:
            cfg = json.load(f)
        missing = [k for k in _REQUIRED_CONFIG_KEYS if not cfg.get(k)]
        if missing:
            raise ValueError(f"Invalid streamer config (missing: {', '.join(missing)})")
        print(f"[OK] Loaded config from {self.config_path}")
        print(
            f" Stream wire: format={cfg.get('format')} "
            f"pix={wire_pixel_format(cfg)} "
            f"detection={'ON' if cfg.get('detection_enabled') else 'OFF'}"
        )
        return cfg
    
    def _find_gst_launch(self):
        """Find gst-launch-1.0 executable (Raspberry Pi)"""
        gst_path = shutil.which('gst-launch-1.0')
        if not gst_path:
            gst_path = '/usr/bin/gst-launch-1.0'
        return gst_path if os.path.exists(gst_path) else None

    def _find_ffmpeg(self):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            ffmpeg = '/usr/bin/ffmpeg'
        return ffmpeg if os.path.exists(ffmpeg) else None

    def _has_gst_rtspclientsink(self):
        if not self.gst_launch_path:
            return False
        inspect = self.gst_launch_path.replace('gst-launch-1.0', 'gst-inspect-1.0')
        if not os.path.exists(inspect):
            return False
        try:
            return subprocess.run(
                [inspect, 'rtspclientsink'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).returncode == 0
        except Exception:
            return False

    def _x264_thread_count(self) -> int:
        """Dual-cam: 1 thread/stream — giảm CPU/RAM contention trên CM5."""
        if self.config.get('multi_camera'):
            return 1
        if os.environ.get('DRONEBRIDGE_MULTI_CAMERA') == '1':
            return 1
        return 2

    def _ffmpeg_raw_rtsp_cmd(self):
        width, height = self.config['size']
        fps = int(self.config['framerate'])
        bitrate = int(self.config.get('bitrate', 5000))
        gop = int(self.config.get('keyframe_interval', 30))
        rtsp_url = self._rtsp_url()
        ffmpeg = self._find_ffmpeg()
        pipe_pix = 'rgb24' if wire_pixel_format(self.config) == 'rgb' else 'bgr24'
        preset = self.config.get('preset', 'veryfast') or 'veryfast'
        tune = self.config.get('tune', 'zerolatency') or 'zerolatency'
        bufsize_k = max(bitrate, 1500)
        threads = self._x264_thread_count()
        return [
            ffmpeg, '-loglevel', 'warning', '-nostats',
            '-f', 'rawvideo', '-pix_fmt', pipe_pix,
            '-s', f'{width}x{height}', '-r', str(fps),
            '-i', 'pipe:0',
            # yuv420p + baseline: MediaMTX WebRTC (see mediamtx.org/docs/features/webrtc-specific-features)
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-profile:v', 'baseline', '-level', '3.1',
            '-preset', preset, '-tune', tune,
            '-threads', str(threads),
            '-b:v', f'{bitrate}k', '-maxrate', f'{bitrate}k', '-bufsize', f'{bufsize_k}k',
            '-g', str(gop), '-keyint_min', str(gop), '-sc_threshold', '0', '-bf', '0',
            '-x264-params', 'repeat-headers=1',
            '-f', 'rtsp', '-rtsp_transport', 'tcp',
            rtsp_url,
        ]

    def _ffmpeg_h264_copy_rtsp_cmd(self):
        rtsp_url = self._rtsp_url()
        ffmpeg = self._find_ffmpeg()
        return [
            ffmpeg, '-loglevel', 'warning', '-nostats',
            '-probesize', '32768', '-analyzeduration', '0',
            '-fflags', '+genpts+igndts+nobuffer',
            '-use_wallclock_as_timestamps', '1',
            '-flags', 'low_delay',
            '-f', 'h264', '-i', 'pipe:0',
            '-c:v', 'copy',
            '-muxdelay', '0', '-muxpreload', '0',
            '-max_interleave_delta', '0',
            '-f', 'rtsp', '-rtsp_transport', 'tcp',
            rtsp_url,
        ]

    def _hw_keyframe_interval(self) -> int:
        gop = int(self.config.get('keyframe_interval', 15) or 15)
        return min(max(gop, 1), 30)

    def _hw_bitrate_kbps(self) -> int:
        return int(self.config.get('bitrate', 4000) or 4000)
    
    def _rtsp_url(self):
        pub = self.config['publish_path']
        return (
            f"rtsp://{self.config['mediamtx_host']}:"
            f"{self.config['mediamtx_port']}{pub}"
        )

    def _viewer_urls(self):
        pub = self.config['publish_path'].rstrip('/')
        host = self.config['mediamtx_host']
        webrtc_port = int(self.config.get('mediamtx_webrtc_port', 8889))
        hls_port = int(self.config.get('mediamtx_hls_port', 8888))
        return {
            'webrtc': f"http://{host}:{webrtc_port}{pub}/whep",
            'hls': f"http://{host}:{hls_port}{pub}/index.m3u8",
            'rtsp': self._rtsp_url(),
        }

    def _should_use_hw_passthrough(self) -> bool:
        """Stream-only CSI: rpicam-vid HW H.264 — tiết kiệm ~200MB RAM/process vs Picamera2 loop."""
        mode = os.environ.get('DRONEBRIDGE_HW_PASSTHROUGH', 'auto').lower()
        if mode in ('0', 'off', 'false', 'no'):
            return False
        if mode in ('1', 'on', 'true', 'yes'):
            return bool(shutil.which('rpicam-vid') or os.path.exists('/usr/bin/rpicam-vid'))
        if self._cv_enabled():
            return False
        return bool(shutil.which('rpicam-vid') or os.path.exists('/usr/bin/rpicam-vid'))

    def _gst_python_encode_cmd(self, fd=0):
        """Build gst-launch argv list (never split URLs on whitespace)."""
        width, height = self.config['size']
        fps = int(self.config['framerate'])
        rtsp_url = self._rtsp_url()
        bitrate = int(self.config.get('bitrate', 5000))
        gop = int(self.config.get('keyframe_interval', 30))
        preset = self.config.get('preset', 'ultrafast')
        tune = self.config.get('tune', 'zerolatency')
        pix_fmt = wire_pixel_format(self.config)
        x264_threads = self._x264_thread_count()
        return [
            self.gst_launch_path, '-e',
            'fdsrc', f'fd={fd}', 'do-timestamp=true', '!',
            'rawvideoparse', f'width={width}', f'height={height}',
            f'format={pix_fmt}', f'framerate={fps}/1', '!',
            'queue', 'max-size-buffers=1', 'max-size-time=0', 'leaky=downstream', '!',
            'videoconvert', '!',
            'video/x-raw,format=I420', '!',
            'x264enc', f'bitrate={bitrate}', f'speed-preset={preset}', f'tune={tune}',
            f'key-int-max={gop}', 'bframes=0', 'rc-lookahead=0', 'byte-stream=true',
            f'threads={x264_threads}', '!',
            'h264parse', 'config-interval=1', '!',
            'queue', 'max-size-buffers=1', 'max-size-time=0', 'leaky=downstream', '!',
            'rtspclientsink', f'location={rtsp_url}', 'protocols=tcp', 'latency=0',
        ]

    def _cv_enabled(self) -> bool:
        return bool(self.config.get('detection_enabled', True) or self.config.get('overlay_enabled', True))

    def _can_use_h264_stream_path(self) -> bool:
        """Hướng 1 thống nhất: Picamera2 HW H264 — không phụ thuộc Video processing."""
        if os.environ.get('DRONEBRIDGE_FORCE_RAW_PIPELINE', '').lower() in ('1', 'true', 'yes'):
            return False
        try:
            from picamera2.encoders import H264Encoder  # noqa: F401
        except ImportError:
            return False
        return True

    def start_h264_stream_pipeline(self):
        """Hướng 1: Picamera2 HW H264 → FFmpeg RTSP. Hướng 2 tùy chọn trong cùng loop."""
        if not self.running.is_set():
            self.running.set()

        cam_id = self.config['camera_id']
        urls = self._viewer_urls()
        cv_on = self._cv_enabled()

        print("=" * 60)
        print(" Camera Streamer — Hướng 1 thống nhất (HW H264 → RTSP)")
        print("=" * 60)
        print(f" Stream cam{cam_id} | {self.config['size'][0]}x{self.config['size'][1]} @ {self.config['framerate']} fps")
        print(f" RTSP: {urls['rtsp']}")
        print(f" Video processing (Hướng 2): {'ON' if cv_on else 'OFF'}")
        print("=" * 60)

        from stream.h264_cv_loop import run_h264_stream_loop

        self._capture_ready.clear()
        self._capture_ok = False
        if not run_h264_stream_loop(self):
            print("[WARN] Hướng 1 HW H264 unavailable — fallback raw capture_loop")
            return self.start_stream_pipeline()

    def start_h264_cv_pipeline(self):
        """Alias — giữ tương thích gọi cũ."""
        return self.start_h264_stream_pipeline()

    def _prefer_ffmpeg_rtsp(self) -> bool:
        """Publisher backend — policy from Go (camera_config_*.json rtsp_backend), env override."""
        backend = os.environ.get('DRONEBRIDGE_RTSP_BACKEND', '').lower()
        if backend == 'ffmpeg':
            return True
        if backend == 'gstreamer':
            return False
        cfg_backend = str(self.config.get('rtsp_backend', '') or '').lower()
        if cfg_backend == 'ffmpeg':
            return True
        if cfg_backend == 'gstreamer':
            return False
        if self.config.get('multi_camera'):
            return True
        return True

    def start_gstreamer(self, pipe_stdin):
        """Start RTSP push encoder. GStreamer first; FFmpeg fallback only."""
        ffmpeg = self._find_ffmpeg()
        prefer_ffmpeg = self._prefer_ffmpeg_rtsp()
        use_gst = self._has_gst_rtspclientsink() and not prefer_ffmpeg
        use_ffmpeg = ffmpeg is not None

        if not use_gst and not use_ffmpeg:
            print("✗ Neither GStreamer rtspclientsink nor ffmpeg available for RTSP push")
            print("  Install: sudo apt install -y gstreamer1.0-rtsp gstreamer1.0-plugins-{good,bad,ugly} ffmpeg")
            return False

        max_retries = 3
        for retry_count in range(max_retries):
            try:
                if use_gst:
                    cmd = self._gst_python_encode_cmd(fd=0)
                    backend = 'GStreamer'
                else:
                    cmd = self._ffmpeg_raw_rtsp_cmd()
                    backend = 'FFmpeg'

                if retry_count == 0:
                    print(f" Using {backend} for RTSP push")
                    print(f" RTSP: {self._rtsp_url()}")
                else:
                    print(f" Retry {retry_count}/{max_retries - 1}...")

                self.gst_process = subprocess.Popen(
                    cmd,
                    stdin=pipe_stdin,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )

                time.sleep(0.8)

                if self.gst_process.poll() is None:
                    print(f" {backend} RTSP encoder running (waiting for frames...)")
                    return True

                _, stderr = self.gst_process.communicate(timeout=2)
                error_msg = stderr.decode(errors='replace').strip() if stderr else 'Unknown error'
                first_line = error_msg.split('\n')[-1] if error_msg else 'Unknown error'
                if retry_count < max_retries - 1:
                    print(f"  Attempt {retry_count + 1} failed: {first_line[:200]}")
                    time.sleep(1)
                else:
                    print(f"✗ {backend} exited with code {self.gst_process.returncode}")
                    print(f"✗ Error: {error_msg[:800]}")
                    return False

            except Exception as e:
                print(f"✗ Failed to start RTSP encoder: {e}")
                return False

        return False
    
    def draw_overlay(self, frame, detection_result):
        """Backward-compatible wrapper — logic in processing.overlay."""
        from processing.overlay import draw_overlay as _draw_overlay

        return _draw_overlay(
            frame, detection_result,
            overlay_enabled=self.config.get('overlay_enabled', True),
        )

    def capture_and_stream_thread(self, pipe_write_fd):
        """Hướng 1: capture → FrameGate → encode (see stream/capture_loop.py)."""
        from stream.capture_loop import run_capture_loop

        run_capture_loop(self, pipe_write_fd)

    def get_direction(self, offset_x, offset_y, threshold=20):
        from processing.detectors.contour_h.detect import get_direction
        return get_direction(offset_x, offset_y, threshold)

    def start_hw_passthrough(self):
        """libcamera hardware H.264 → GStreamer → RTSP (lowest latency path)."""
        if not self.running.is_set():
            self.running.set()

        rpicam = shutil.which('rpicam-vid') or '/usr/bin/rpicam-vid'
        if not os.path.exists(rpicam):
            print("[WARN] rpicam-vid not found — falling back to unified stream pipeline")
            return self.start_stream_pipeline()

        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            print("[WARN] ffmpeg not found — falling back to unified stream pipeline")
            return self.start_stream_pipeline()

        cam_id = int(self.config.get('libcamera_index', self.config['camera_id']))
        enc = self.config.get('encode_size') or self.config['size']
        width, height = enc[0], enc[1]
        fps = int(self.config['framerate'])
        buf_count = int(self.config.get('buffer_count') or 3)
        if buf_count < 1:
            buf_count = 1
        if buf_count > 6:
            buf_count = 6
        rtsp_url = self._rtsp_url()
        urls = self._viewer_urls()

        print("=" * 60)
        print(" HW Passthrough (libcamera H.264 → RTSP, no Python frame loop)")
        print("=" * 60)
        print(f" Camera index: {cam_id}")
        print(f" Resolution: {width}x{height} @ {fps} fps (buffer={buf_count})")
        print(f" RTSP: {rtsp_url}")
        print("=" * 60)

        ffmpeg_cmd = self._ffmpeg_h264_copy_rtsp_cmd()

        rpicam_cmd = [
            rpicam, '-t', '0', '-n',
            '--camera', str(cam_id),
            '--width', str(width), '--height', str(height),
            '--framerate', str(fps),
            '--codec', 'h264', '--inline', '--flush',
            '--libav-format', 'h264',
            '--profile', 'baseline',
            '-b', f'{self._hw_bitrate_kbps()}k',
            '-g', str(self._hw_keyframe_interval()),
            '--buffer-count', str(buf_count),
            '-o', '-',
        ]

        try:
            self.gst_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.rpicam_process = subprocess.Popen(
                rpicam_cmd,
                stdout=self.gst_process.stdin,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.gst_process.stdin.close()
        except Exception as e:
            print(f"✗ HW passthrough start failed: {e}")
            self.running.clear()
            return

        time.sleep(1.5)
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if self.rpicam_process.poll() is not None:
                print("✗ rpicam-vid exited early — using unified Picamera2 stream pipeline")
                self.stop()
                return self.start_stream_pipeline()
            if self.gst_process.poll() is not None:
                print("✗ FFmpeg RTSP push exited early — using unified Picamera2 stream pipeline")
                self.stop()
                return self.start_stream_pipeline()
            time.sleep(0.25)

        print("\n HW passthrough streaming started")
        print(f"   WebRTC: {urls['webrtc']}")
        print(f"   HLS: {urls['hls']}")
        print("\nPress Ctrl+C to stop...\n")

        try:
            while self.running.is_set():
                if self.rpicam_process and self.rpicam_process.poll() is not None:
                    print("\n rpicam-vid stopped unexpectedly")
                    break
                if self.gst_process and self.gst_process.poll() is not None:
                    print("\n FFmpeg RTSP push stopped unexpectedly")
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n Stopping...")
        finally:
            self.stop()

    def start_stream_pipeline(self):
        """Picamera2 → capture_loop → RTSP publish (same pipeline for cam0 and cam1)."""
        try:
            import cv2  # noqa: F401
        except ImportError:
            print("✗ OpenCV (python3-opencv) required")
            print("  Install: sudo apt install -y python3-opencv python3-picamera2")
            return
        if not self.running.is_set():
            self.running.set()

        os_name = platform.system()
        cam_id = self.config['camera_id']
        lib_idx = self.config.get('libcamera_index', cam_id)
        urls = self._viewer_urls()

        print("=" * 60)
        print(" Camera Streamer (unified: Picamera2 → GStreamer RTSP)")
        print("=" * 60)
        print(f" Platform: {os_name}")
        print(f" Stream cam{cam_id} | libcamera index {lib_idx}")
        print(f" Resolution: {self.config['size'][0]}x{self.config['size'][1]} @ {self.config['framerate']} fps")
        print(f" Server: {self.config['mediamtx_host']}:{self.config['mediamtx_port']}")
        print(f" Detection: {'ON' if self.config.get('detection_enabled') else 'OFF'} | "
              f"Overlay: {'ON' if self.config.get('overlay_enabled') else 'OFF'}")
        print("=" * 60)
        
        # Create pipe for stdin streaming (works on Windows and Linux)
        pipe_read, pipe_write = os.pipe()
        self.pipe_read_fd = pipe_read
        
        # Start capture thread; wait for camera init before starting encoder
        self._capture_ready.clear()
        self._capture_ok = False
        capture_thread = Thread(target=self.capture_and_stream_thread, args=(pipe_write,), daemon=False)
        capture_thread.start()

        if not self._capture_ready.wait(timeout=8):
            print("✗ Capture thread did not report camera status in time")
            self.running.clear()
            try:
                os.close(pipe_read)
                os.close(pipe_write)
            except OSError:
                pass
            capture_thread.join(timeout=3)
            return

        if not self._capture_ok:
            print("✗ Camera unavailable — not starting RTSP encoder")
            self.running.clear()
            try:
                os.close(pipe_read)
                os.close(pipe_write)
            except OSError:
                pass
            capture_thread.join(timeout=3)
            return

        # Start GStreamer to stream from pipe stdin
        if not self.start_gstreamer(pipe_read):
            self.running.clear()
            try:
                os.close(pipe_read)
                os.close(pipe_write)
            except:
                pass
            return
        
        print("\n Camera streaming started")
        print(f" View at:")
        print(f"   - WebRTC: {urls['webrtc']}")
        print(f"   - HLS: {urls['hls']}")
        print(f"   - RTSP: {urls['rtsp']}")
        print("\nPress Ctrl+C to stop...\n")
        
        # Wait for interrupt
        try:
            while self.running.is_set() and self.gst_process and self.gst_process.poll() is None:
                time.sleep(1)
            
            if self.gst_process and self.gst_process.poll() is not None:
                print(f"\n️  GStreamer stopped unexpectedly (exit code: {self.gst_process.returncode})")
                self.running.clear()
                
        except KeyboardInterrupt:
            print("\n Stopping camera streamer...")
            self.stop()
        finally:
            try:
                os.close(pipe_read)
                os.close(pipe_write)
            except:
                pass
            capture_thread.join(timeout=5)

    # Giữ tên cũ — alias tới pipeline thống nhất.
    start_python_pipeline = start_stream_pipeline

    def start(self):
        """Entry — cam0/cam1 cùng Hướng 1; Video processing chỉ bật Hướng 2."""
        if self._can_use_h264_stream_path():
            self.start_h264_stream_pipeline()
        else:
            print("[WARN] Picamera2 HW H264 không khả dụng — fallback capture_loop")
            self.start_stream_pipeline()

    def stop(self):
        """Stop camera streaming"""
        if not self.running.is_set() and self.gst_process is None:
            return

        print("\n Stopping camera streamer...")
        self.running.clear()

        if self.rpicam_process:
            try:
                self.rpicam_process.terminate()
                self.rpicam_process.wait(timeout=3)
            except Exception:
                self.rpicam_process.kill()
            finally:
                self.rpicam_process = None

        # Stop encoder (GStreamer/FFmpeg) and its process group
        if self.gst_process:
            try:
                if self.gst_process.poll() is None:
                    try:
                        os.killpg(os.getpgid(self.gst_process.pid), signal.SIGTERM)
                    except Exception:
                        self.gst_process.terminate()
                    self.gst_process.wait(timeout=5)
                print(" Encoder stopped")
            except Exception:
                try:
                    os.killpg(os.getpgid(self.gst_process.pid), signal.SIGKILL)
                except Exception:
                    self.gst_process.kill()
                print(" Encoder killed")
            finally:
                self.gst_process = None
        
        print(" Camera streamer stopped")


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    global _shutdown_requested
    if _shutdown_requested:
        print("\n Force exit")
        os._exit(1)

    _shutdown_requested = True
    print("\n Signal received, stopping...")

    try:
        if _active_streamer is not None:
            _active_streamer.stop()
    finally:
        os._exit(0)


def main():
    """Main entry point"""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    
    config_path = sys.argv[1] if len(sys.argv) > 1 else 'camera_config_0.json'
    
    global _active_streamer
    try:
        streamer = CameraStreamer(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"✗ {e}")
        sys.exit(1)
    _active_streamer = streamer
    streamer.start()


if __name__ == '__main__':
    main()

