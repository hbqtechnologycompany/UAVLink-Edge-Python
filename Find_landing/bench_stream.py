#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Benchmark camera stream trên thiết kế hiện tại (Picamera2 → pipe → FFmpeg/GStreamer → RTSP).

Kiểm tra 3 tầng:
  1. Capture thuần (Picamera2) — camera hardware
  2. Publisher (camera_streamer Stats) — FPS Pi thực sự gửi
  3. RTSP output (ffprobe) — FPS server nhận (chuẩn để so với QCC viewer)
"""
import json
import os
import re
import subprocess
import sys
import time

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'camera_config_0.json')
TARGET_FPS = 30
MIN_OK_FPS = 24  # cho phép ~20% headroom dưới target


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def bench_capture_only(seconds=5):
    sys.path.insert(0, os.path.dirname(__file__))
    from camera_manager import get_camera_manager

    cfg = load_config()
    cm = get_camera_manager()
    cam_cfg = {
        'format': cfg.get('format', 'BGR888'),
        'size': tuple(cfg['size']),
    }
    if not cm.get_camera(cfg['camera_id'], 'bench', cam_cfg):
        return {'ok': False, 'error': 'camera init failed'}

    frames = 0
    start = time.time()
    while time.time() - start < seconds:
        if cm.capture_frame(cfg['camera_id'], 'bench') is not None:
            frames += 1
    elapsed = time.time() - start
    cm.release_camera(cfg['camera_id'], 'bench')
    fps = frames / elapsed if elapsed else 0
    return {
        'ok': fps >= MIN_OK_FPS,
        'fps': round(fps, 1),
        'frames': frames,
        'seconds': round(elapsed, 2),
    }


def bench_publisher_stats(seconds=20):
    cfg = load_config()
    log_path = '/tmp/bench_cam0.log'
    cmd = [
        'timeout', str(seconds + 5),
        'python3', 'camera_streamer.py', 'camera_config_0.json',
    ]
    with open(log_path, 'w') as log:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(__file__),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(seconds)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    text = open(log_path).read()
    stats = re.findall(r'Stats:\s+(\d+)\s+frames\s+@\s+([\d.]+)\s+fps', text)
    mismatches = text.count('Frame size mismatch')
    write_fail = text.count('Failed to write full frame')
    first_frame = 'First frame streamed!' in text

    if stats:
        last_frames, last_fps = stats[-1]
        fps = float(last_fps)
    else:
        fps = 0.0
        last_frames = 0

    return {
        'ok': fps >= MIN_OK_FPS and mismatches == 0 and write_fail == 0 and first_frame,
        'fps': fps,
        'frames_sent': int(last_frames),
        'stats_samples': len(stats),
        'frame_size_mismatch': mismatches,
        'write_failures': write_fail,
        'first_frame': first_frame,
        'log_tail': text[-600:],
    }


def bench_rtsp(seconds=10):
    cfg = load_config()
    pub = cfg.get('publish_path', '').strip()
    if not pub.startswith('/'):
        pub = '/' + pub
    url = f"rtsp://{cfg['mediamtx_host']}:{cfg['mediamtx_port']}{pub}"

    try:
        meta = subprocess.check_output([
            'ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=avg_frame_rate,r_frame_rate,width,height,codec_name',
            '-of', 'json', url,
        ], text=True, timeout=15)
        info = json.loads(meta)['streams'][0]
        avg = info.get('avg_frame_rate', '0/0')
        num, den = avg.split('/')
        fps = float(num) / float(den) if float(den) else 0

        count_out = subprocess.check_output([
            'timeout', str(seconds + 2),
            'ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
            '-select_streams', 'v:0', '-count_frames',
            '-show_entries', 'stream=nb_read_frames,duration',
            '-of', 'json', url,
        ], text=True, timeout=seconds + 10)
        cinfo = json.loads(count_out)['streams'][0]
        nb = int(cinfo.get('nb_read_frames', 0))
        dur = float(cinfo.get('duration', 0) or 0)
        measured = nb / dur if dur > 0 else fps

        return {
            'ok': fps >= MIN_OK_FPS and measured >= MIN_OK_FPS,
            'url': url,
            'avg_frame_rate': avg,
            'measured_fps': round(measured, 1),
            'nb_read_frames': nb,
            'duration': round(dur, 2),
            'codec': info.get('codec_name'),
            'resolution': f"{info.get('width')}x{info.get('height')}",
        }
    except subprocess.CalledProcessError as e:
        return {'ok': False, 'error': str(e), 'url': url}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'url': url}


def main():
    print('=' * 60)
    print('Camera Stream Benchmark (existing architecture)')
    print('=' * 60)

    # Stop existing streamers to avoid camera busy
    subprocess.run(['pkill', '-f', 'camera_streamer.py'], stderr=subprocess.DEVNULL)
    time.sleep(2)

    print('\n[1/3] Capture-only (Picamera2)...')
    r1 = bench_capture_only()
    print(f"  FPS={r1.get('fps')}  ok={r1.get('ok')}  {r1.get('frames', '')} frames")

    print('\n[2/3] Publisher (camera_streamer.py Stats)...')
    r2 = bench_publisher_stats()
    print(f"  FPS={r2.get('fps')}  ok={r2.get('ok')}  mismatches={r2.get('frame_size_mismatch')}")

    print('\n[3/3] RTSP output (ffprobe)...')
    r3 = bench_rtsp()
    print(f"  measured={r3.get('measured_fps')}  avg={r3.get('avg_frame_rate')}  ok={r3.get('ok')}")

    all_ok = r1.get('ok') and r2.get('ok') and r3.get('ok')
    print('\n' + '=' * 60)
    print(f"RESULT: {'PASS' if all_ok else 'FAIL'} (target>={MIN_OK_FPS} fps, no frame corruption)")
    print('=' * 60)

    if not r2.get('ok') and r2.get('log_tail'):
        print('\nPublisher log tail:\n', r2['log_tail'])

    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
