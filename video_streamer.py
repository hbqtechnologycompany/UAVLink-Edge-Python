import subprocess
import threading
import logging
import time
import os

logger = logging.getLogger("VideoStreamer")

class VideoStreamer:
    """
    Stream video từ Pi Camera hoặc USB camera lên MediaMTX server.
    MediaMTX nhận RTSP và re-publish sang WebRTC WHEP cho frontend.
    
    Cameras supported:
      Pi Camera Module 3: libcamera-vid (tốt nhất, hardware H.264)
      USB webcam:         ffmpeg v4l2 (fallback, CPU encode)
    """

    def __init__(self, config):
        # config is a Config object, but used here as dict-like via config.video
        self.cfg = getattr(config, 'video', {}) if hasattr(config, 'video') else {}
        if not isinstance(self.cfg, dict):
            # If it's a custom object from config.py
            self.cfg = self.cfg if isinstance(self.cfg, dict) else {}

        self.proc = None
        self.running = False

        # Lấy từ config.yaml
        self.source = self.cfg.get('source', 'picamera')
        self.rtsp_url = self.cfg.get('mediamtx_rtsp', 'rtsp://45.117.171.237:8554')
        self.drone_id = self.cfg.get('stream_name', 'drone1')
        self.width = self.cfg.get('width', 1280)
        self.height = self.cfg.get('height', 720)
        self.fps = self.cfg.get('fps', 30)
        self.bitrate = self.cfg.get('bitrate_kbps', 2000)

    def _build_cmd(self):
        target = f"{self.rtsp_url}/{self.drone_id}"
        if self.source == 'picamera':
            # Pi Camera Module 3: hardware H.264, thấp nhất latency
            # Note: libcamera-vid directly supports RTSP/UDP but using pipe to ffmpeg can be more robust for some MediaMTX setups
            return [
                'libcamera-vid',
                '--width', str(self.width), '--height', str(self.height),
                '--framerate', str(self.fps),
                '--bitrate', str(self.bitrate * 1000),
                '--codec', 'h264', '--profile', 'main',
                '--level', '4.2', '--inline',
                '-t', '0',  # stream mãi mãi
                '-o', '-',  # stdout
                '|',
                'ffmpeg', '-re', '-f', 'h264', '-i', 'pipe:0',
                '-c:v', 'copy', '-f', 'rtsp',
                '-rtsp_transport', 'tcp', target
            ]
        else:  # usb webcam
            return [
                'ffmpeg', '-f', 'v4l2',
                '-video_size', f'{self.width}x{self.height}',
                '-framerate', str(self.fps),
                '-i', self.cfg.get('usb_device', '/dev/video0'),
                '-c:v', 'h264_v4l2m2m',  # Pi hardware encode
                '-b:v', f'{self.bitrate}k',
                '-f', 'rtsp', '-rtsp_transport', 'tcp', target
            ]

    def start(self):
        if not self.cfg:
            logger.warning("Video streaming disabled: No 'video' section in config")
            return
        self.running = True
        threading.Thread(target=self._stream_loop, daemon=True).start()

    def _stream_loop(self):
        while self.running:
            cmd_list = self._build_cmd()
            cmd_str = ' '.join(cmd_list)
            logger.info(f"Starting stream: {self.source} → {self.rtsp_url}/{self.drone_id}")
            try:
                # Dùng shell=True để hỗ trợ pipe trong libcamera cmd
                self.proc = subprocess.Popen(
                    cmd_str, shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE
                )
                self.proc.wait()
            except Exception as e:
                logger.error(f"Stream error: {e}")
            
            if self.running:
                logger.warning("Stream stopped, restarting in 5s...")
                time.sleep(5)  # auto-restart on crash

    def stop(self):
        self.running = False
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except:
                self.proc.kill()
