"""Dual-camera service — port of Pi_CM5 DroneBridge camera/*.go + config_store camera APIs."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from paths import find_landing_path, project_path

logger = logging.getLogger("CameraAPI")

_CAM_LINE_RE = re.compile(
    r"^\s*(\d+)\s*:\s*(\S+)\s*\[([^\]]+)\].*i2c@([0-9a-f]+)/(\w+)@",
    re.MULTILINE,
)
_I2C_TO_PORT = {
    "88000": ("cam0", "J30"),
    "70000": ("cam1", "J29"),
}

_manager_lock = threading.Lock()
_manager_procs: List[Dict[str, Any]] = []


def _find_landing() -> Path:
    return find_landing_path()


def _registry_path() -> Path:
    return _find_landing() / "camera_registry.json"


def _detected_path() -> Path:
    return _find_landing() / "camera_detected.json"


def _stream_config_path(camera_id: int) -> Path:
    return _find_landing() / f"camera_config_{camera_id}.json"


def _default_detected() -> dict:
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "ports": {
            "cam0": {"overlay": "imx219", "sensor": "imx219", "enabled": True},
            "cam1": {"overlay": "imx219", "sensor": "imx219", "enabled": True},
        },
        "last_connected": [],
    }


def _load_detected() -> dict:
    path = _detected_path()
    if not path.exists():
        return _default_detected()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("ports", _default_detected()["ports"])
        return data
    except json.JSONDecodeError:
        return _default_detected()


def _save_detected(data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _detected_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_registry() -> tuple:
    path = _registry_path()
    if not path.exists():
        return {"success": False, "message": f"camera_registry.json not found: {path}"}, 404
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
        return {"success": True, "registry": reg}, 200
    except json.JSONDecodeError as exc:
        return {"success": False, "message": str(exc)}, 500


def _sensor_display_name(reg: Optional[dict], sensor_id: str) -> str:
    if reg:
        for item in reg.get("sensors", []):
            if item.get("id") == sensor_id:
                return f"{item.get('name', sensor_id)} ({sensor_id.upper()})"
    return sensor_id.upper()


def _camera_binary() -> str:
    for name in ("rpicam-hello", "libcamera-hello"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError("rpicam-hello/libcamera-hello not found in PATH")


def _parse_rpicam_list() -> List[dict]:
    binary = _camera_binary()
    result = subprocess.run(
        [binary, "--list-cameras"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    if "No cameras available" in text:
        return []
    if result.returncode != 0 and not text.strip():
        raise RuntimeError(f"rpicam list failed: {result.stderr or result.stdout}")

    reg = None
    if _registry_path().exists():
        try:
            reg = json.loads(_registry_path().read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    cameras = []
    for match in _CAM_LINE_RE.finditer(text):
        idx = int(match.group(1))
        sensor = match.group(2)
        res = match.group(3).split()[0]
        i2c = match.group(4)
        port_info = _I2C_TO_PORT.get(i2c, ("unknown", "?"))
        cameras.append(
            {
                "libcamera_index": idx,
                "sensor": sensor,
                "sensor_name": _sensor_display_name(reg, sensor),
                "physical_port": port_info[0],
                "connector": port_info[1],
                "i2c_bus": i2c,
                "resolution": res,
                "status": "ok",
            }
        )
    return cameras


def _build_detect_result(saved: dict, connected: List[dict], reg: Optional[dict]) -> dict:
    ports: Dict[str, dict] = {}
    index_by_i2c = {c["i2c_bus"]: c["libcamera_index"] for c in connected}
    expected = 0
    for port_name in ("cam0", "cam1"):
        cfg = saved.get("ports", {}).get(port_name, {})
        if not cfg.get("enabled", True):
            continue
        expected += 1
        i2c, conn = ("88000", "J30") if port_name == "cam0" else ("70000", "J29")
        ps = {
            **cfg,
            "physical_port": port_name,
            "connector": conn,
            "i2c_bus": i2c,
        }
        if i2c in index_by_i2c:
            ps["status"] = "ok"
            ps["libcamera_index"] = index_by_i2c[i2c]
        else:
            ps["status"] = "error"
            ps["message"] = "Không probe được sensor trên cổng này (kiểm tra cáp / loại module / overlay)"
        ports[port_name] = ps

    supported = len(reg.get("sensors", [])) if reg else 0
    return {
        "success": True,
        "connected_count": len(connected),
        "expected_count": expected,
        "supported_types": supported,
        "updated_at": saved.get("updated_at"),
        "cached": False,
        "connected": connected,
        "ports": ports,
    }


def camera_detect(refresh: bool = False) -> tuple:
    saved = _load_detected()
    reg = None
    if _registry_path().exists():
        try:
            reg = json.loads(_registry_path().read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    connected: List[dict] = []
    scan_err = None
    try:
        connected = _parse_rpicam_list()
    except Exception as exc:
        scan_err = str(exc)

    if refresh or connected:
        saved["last_connected"] = connected
        _save_detected(saved)

    result = _build_detect_result(saved, connected, reg)
    result["cached"] = not refresh
    if scan_err:
        result["message"] = scan_err
    return result, 200


def camera_ports_save(ports: dict) -> tuple:
    saved = _load_detected()
    for key, val in (ports or {}).items():
        if not isinstance(val, dict):
            continue
        if not val.get("sensor"):
            val["sensor"] = val.get("overlay", "")
        saved.setdefault("ports", {})[key] = val
    _save_detected(saved)
    return {
        "success": True,
        "message": "Đã lưu loại CAM. Chạy: sudo bash setup_camera.sh && sudo reboot",
    }, 200


def _ensure_default_streams(cfg) -> None:
    camera = cfg.data.setdefault("camera", {})
    if camera.get("streams"):
        return
    camera["streams"] = [
        {
            "name": "cam0",
            "camera_id": 0,
            "enabled": True,
            "size": [640, 480],
            "framerate": 30,
            "bitrate": 5000,
            "format": "RGB888",
            "brightness": 0,
            "contrast": 1,
            "exposure_time": 0,
            "detection_enabled": True,
            "overlay_enabled": True,
            "keyframe_interval": 30,
            "preset": "ultrafast",
            "tune": "zerolatency",
        },
        {
            "name": "cam1",
            "camera_id": 1,
            "enabled": True,
            "size": [640, 480],
            "framerate": 30,
            "bitrate": 5000,
            "format": "RGB888",
            "brightness": 0,
            "contrast": 1,
            "exposure_time": 0,
            "detection_enabled": False,
            "overlay_enabled": False,
            "keyframe_interval": 30,
            "preset": "ultrafast",
            "tune": "zerolatency",
        },
    ]


def _mediamtx(cfg) -> dict:
    camera = cfg.data.get("camera", {})
    auth = cfg.auth if isinstance(cfg.auth, dict) else {}
    host = camera.get("mediamtx_host") or auth.get("host") or "10.8.0.1"
    return {
        "host": host,
        "rtsp_port": int(camera.get("mediamtx_port", 8554)),
        "webrtc_port": int(camera.get("mediamtx_webrtc_port", 8889)),
        "hls_port": int(camera.get("mediamtx_hls_port", 8888)),
    }


def publish_path(cfg, camera_id: int) -> str:
    auth = cfg.auth if isinstance(cfg.auth, dict) else {}
    drone_id = auth.get("uuid") or ""
    if not drone_id:
        raise ValueError("auth.uuid is required for camera publish path")
    camera = cfg.data.get("camera", {})
    if camera_id == 0:
        suffix = camera.get("publish_path_cam0_suffix") or "cam0"
    elif camera_id == 1:
        suffix = camera.get("publish_path_cam1_suffix") or "cam1"
    else:
        suffix = f"cam{camera_id}"
    return f"/{quote(drone_id, safe='')}/{suffix}"


def _find_stream(cfg, camera_id: int) -> tuple:
    _ensure_default_streams(cfg)
    streams = cfg.data.get("camera", {}).get("streams", [])
    for idx, stream in enumerate(streams):
        if int(stream.get("camera_id", -1)) == camera_id:
            return idx, stream
    default = {
        "name": f"cam{camera_id}",
        "camera_id": camera_id,
        "enabled": True,
        "size": [640, 480],
        "framerate": 30,
        "bitrate": 5000,
        "format": "RGB888",
        "brightness": 0,
        "contrast": 1,
        "exposure_time": 0,
        "detection_enabled": camera_id == 0,
        "overlay_enabled": camera_id == 0,
        "keyframe_interval": 30,
        "preset": "ultrafast",
        "tune": "zerolatency",
    }
    return -1, default


def camera_config_to_ui(cfg, camera_id: int) -> dict:
    _, stream = _find_stream(cfg, camera_id)
    m = _mediamtx(cfg)
    pub = publish_path(cfg, camera_id)
    size = stream.get("size") or [640, 480]
    return {
        "camera_id": camera_id,
        "name": stream.get("name", f"cam{camera_id}"),
        "enabled": stream.get("enabled", True),
        "size": size,
        "framerate": int(stream.get("framerate", 30)),
        "format": stream.get("format", "RGB888"),
        "mediamtx_host": m["host"],
        "mediamtx_port": m["rtsp_port"],
        "mediamtx_webrtc_port": m["webrtc_port"],
        "mediamtx_hls_port": m["hls_port"],
        "publish_path": pub,
        "drone_id": cfg.auth.get("uuid", "") if isinstance(cfg.auth, dict) else "",
        "bitrate": int(stream.get("bitrate", 5000)),
        "overlay_enabled": bool(stream.get("overlay_enabled", camera_id == 0)),
        "detection_enabled": bool(stream.get("detection_enabled", camera_id == 0)),
        "keyframe_interval": int(stream.get("keyframe_interval", 30)),
        "preset": stream.get("preset", "ultrafast"),
        "tune": stream.get("tune", "zerolatency"),
        "brightness": float(stream.get("brightness", 0)),
        "contrast": float(stream.get("contrast", 1)),
        "exposure_time": int(stream.get("exposure_time", 0)),
    }


def camera_streams_summary(cfg) -> List[dict]:
    _ensure_default_streams(cfg)
    out = []
    for stream in cfg.data.get("camera", {}).get("streams", []):
        size = stream.get("size") or [640, 480]
        out.append(
            {
                "camera_id": stream.get("camera_id"),
                "name": stream.get("name"),
                "enabled": stream.get("enabled", True),
                "size": size,
                "format": stream.get("format", "RGB888"),
            }
        )
    return out


def stream_endpoints(cfg) -> List[dict]:
    _ensure_default_streams(cfg)
    m = _mediamtx(cfg)
    out = []
    for stream in cfg.data.get("camera", {}).get("streams", []):
        if not stream.get("enabled", True):
            continue
        cam_id = int(stream.get("camera_id", 0))
        pub = publish_path(cfg, cam_id)
        out.append(
            {
                "camera_id": cam_id,
                "name": stream.get("name", f"cam{cam_id}"),
                "publish_path": pub,
                "rtsp": f"rtsp://{m['host']}:{m['rtsp_port']}{pub}",
                "webrtc_whep": f"http://{m['host']}:{m['webrtc_port']}{pub}/whep",
                "hls": f"http://{m['host']}:{m['hls_port']}{pub}/index.m3u8",
            }
        )
    return out


def write_streamer_configs(cfg) -> List[str]:
    _ensure_default_streams(cfg)
    m = _mediamtx(cfg)
    drone_id = cfg.auth.get("uuid", "") if isinstance(cfg.auth, dict) else ""
    if not drone_id:
        raise ValueError("auth.uuid is required to publish camera streams")

    written = []
    landing = _find_landing()
    landing.mkdir(parents=True, exist_ok=True)
    for stream in cfg.data.get("camera", {}).get("streams", []):
        if not stream.get("enabled", True):
            continue
        cam_id = int(stream.get("camera_id", 0))
        pub = publish_path(cfg, cam_id)
        size = stream.get("size") or [640, 480]
        payload = {
            "camera_id": cam_id,
            "size": size,
            "framerate": int(stream.get("framerate", 30)),
            "format": stream.get("format", "RGB888"),
            "mediamtx_host": m["host"],
            "mediamtx_port": m["rtsp_port"],
            "mediamtx_webrtc_port": m["webrtc_port"],
            "mediamtx_hls_port": m["hls_port"],
            "publish_path": pub,
            "drone_id": drone_id,
            "bitrate": int(stream.get("bitrate", 5000)),
            "overlay_enabled": bool(stream.get("overlay_enabled", cam_id == 0)),
            "detection_enabled": bool(stream.get("detection_enabled", cam_id == 0)),
            "keyframe_interval": int(stream.get("keyframe_interval", 30)),
            "preset": stream.get("preset", "ultrafast"),
            "tune": stream.get("tune", "zerolatency"),
            "brightness": float(stream.get("brightness", 0)),
            "contrast": float(stream.get("contrast", 1)),
            "exposure_time": int(stream.get("exposure_time", 0)),
        }
        path = _stream_config_path(cam_id)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(str(path))
    return written


def save_camera_stream_from_ui(cfg, incoming: dict) -> None:
    camera_id = int(incoming.get("camera_id", 0))
    idx, stream = _find_stream(cfg, camera_id)
    if incoming.get("name"):
        stream["name"] = incoming["name"]
    if "enabled" in incoming:
        stream["enabled"] = bool(incoming["enabled"])
    if "format" in incoming:
        stream["format"] = incoming["format"]
    if "size" in incoming:
        stream["size"] = incoming["size"]
    for key in ("framerate", "bitrate", "exposure_time", "keyframe_interval"):
        if key in incoming:
            stream[key] = int(incoming[key])
    for key in ("brightness", "contrast"):
        if key in incoming:
            stream[key] = float(incoming[key])
    for key in ("detection_enabled", "overlay_enabled"):
        if key in incoming:
            stream[key] = bool(incoming[key])
    for key in ("preset", "tune"):
        if key in incoming:
            stream[key] = incoming[key]

    streams = cfg.data.setdefault("camera", {}).setdefault("streams", [])
    if idx >= 0:
        streams[idx] = stream
    else:
        streams.append(stream)
    cfg.save()
    try:
        write_streamer_configs(cfg)
    except ValueError as exc:
        logger.info("[CAMERA] Streamer JSON skipped: %s", exc)


def _proc_alive(proc: Optional[subprocess.Popen]) -> bool:
    return proc is not None and proc.poll() is None


def _stop_manager_locked() -> None:
    global _manager_procs
    for item in _manager_procs:
        proc = item.get("proc")
        if proc and _proc_alive(proc):
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    _manager_procs = []


def camera_restart(cfg) -> tuple:
    try:
        paths = write_streamer_configs(cfg)
    except ValueError as exc:
        with _manager_lock:
            _stop_manager_locked()
        return {
            "success": True,
            "message": str(exc),
            "pids": [],
            "streams_started": 0,
        }, 200

    with _manager_lock:
        _stop_manager_locked()
        if not paths:
            return {
                "success": True,
                "message": "Không có camera stream bật — đã dừng streamer",
                "pids": [],
                "streams_started": 0,
            }, 200

        script = _find_landing() / "camera_streamer.py"
        if not script.exists():
            return {"success": False, "message": "camera_streamer.py not found"}, 503

        started = []
        for i, cfg_path in enumerate(sorted(paths)):
            if i > 0:
                time.sleep(8)
            base = Path(cfg_path).name
            match = re.search(r"(\d+)", base)
            cam_id = int(match.group(1)) if match else i
            env = os.environ.copy()
            if len(paths) > 1:
                env["DRONEBRIDGE_MULTI_CAMERA"] = "1"
            proc = subprocess.Popen(
                [sys.executable, str(script), base],
                cwd=str(_find_landing()),
                env=env,
            )
            started.append({"camera_id": cam_id, "proc": proc, "config": base})
            logger.info("[CAMERA] Streamer cam%s started (PID %s)", cam_id, proc.pid)

        _manager_procs.extend(started)
        time.sleep(2)

    pids = camera_running_pids()
    return {
        "success": True,
        "message": "Camera streamer restarted",
        "pids": pids,
        "streams_started": len(pids),
    }, 200


def camera_running_pids() -> List[int]:
    with _manager_lock:
        return [
            item["proc"].pid
            for item in _manager_procs
            if _proc_alive(item.get("proc"))
        ]


def camera_stream_status() -> List[dict]:
    with _manager_lock:
        out = []
        for item in _manager_procs:
            proc = item.get("proc")
            alive = _proc_alive(proc)
            out.append(
                {
                    "camera_id": item.get("camera_id"),
                    "running": alive,
                    "pid": proc.pid if alive and proc else 0,
                }
            )
        return out


def camera_status_full(cfg) -> dict:
    pids = camera_running_pids()
    return {
        "running": len(pids) > 0,
        "pid": pids[0] if pids else 0,
        "pids": pids,
        "streams": camera_stream_status(),
        "endpoints": stream_endpoints(cfg) if cfg else [],
    }


def camera_config_load(cfg, camera_id: int = 0) -> tuple:
    if cfg is None:
        return {"success": False, "message": "Config not loaded"}, 500
    _ensure_default_streams(cfg)
    return {
        "success": True,
        "config": camera_config_to_ui(cfg, camera_id),
        "streams": camera_streams_summary(cfg),
        "path": str(project_path("config.yaml")),
    }, 200


def camera_config_save(cfg, incoming: dict, restart: bool = False) -> tuple:
    if cfg is None:
        return {"success": False, "message": "Config not loaded"}, 500
    save_camera_stream_from_ui(cfg, incoming)
    restart_msg = ""
    if restart or incoming.get("restart"):
        res, code = camera_restart(cfg)
        if not res.get("success"):
            return res, code
        restart_msg = " (stream restarted)"
    cam_id = int(incoming.get("camera_id", 0))
    return {
        "success": True,
        "message": f"Camera config saved{restart_msg}",
        "config": camera_config_to_ui(cfg, cam_id),
        "path": str(project_path("config.yaml")),
    }, 200


def _temp_json_path(name: str) -> Path:
    return Path("/tmp") / name


def read_landing_telemetry(camera_id: int, max_age_sec: float = 10.0) -> Optional[dict]:
    path = _temp_json_path(f"camera_landing_{camera_id}.json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    updated_at = float(data.get("updated_at", 0) or 0)
    if updated_at > 0:
        age = time.time() - updated_at
        data["age_sec"] = age
        if max_age_sec > 0 and age > max_age_sec:
            return None
    return data


def read_stream_stats(camera_id: int, max_age_sec: float = 10.0) -> Optional[dict]:
    path = _temp_json_path(f"camera_stream_stats_{camera_id}.json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    updated_at = float(data.get("updated_at", 0) or 0)
    if updated_at > 0:
        age = time.time() - updated_at
        data["age_sec"] = age
        if max_age_sec > 0 and age > max_age_sec:
            return None
    if float(data.get("fps_window", 0) or 0) < 0.5:
        return None
    return data


def landing_status_for_config(cfg, max_age_sec: float = 10.0) -> List[dict]:
    _ensure_default_streams(cfg)
    out = []
    for stream in cfg.camera.get("streams", []):
        if not stream.get("enabled", True):
            continue
        cam_id = int(stream.get("camera_id", 0))
        entry = {"camera_id": cam_id, "name": stream.get("name", f"cam{cam_id}")}
        landing = read_landing_telemetry(cam_id, max_age_sec)
        if landing:
            entry["landing"] = landing
        stats = read_stream_stats(cam_id, max_age_sec)
        if stats:
            entry["stream_stats"] = stats
        out.append(entry)
    return out


def camera_global_payload(cfg) -> dict:
    camera = cfg.camera if cfg else {}
    return {
        "success": True,
        "primary_camera_id": int(camera.get("primary_camera_id", 0)),
        "auto_memory_profile": bool(camera.get("auto_memory_profile", True)),
        "memory_tier_gb": _detect_memory_tier_gb(),
    }


def save_camera_global_from_ui(cfg, incoming: dict) -> None:
    camera = cfg.data.setdefault("camera", {})
    if "primary_camera_id" in incoming:
        camera["primary_camera_id"] = int(incoming["primary_camera_id"])
    if "auto_memory_profile" in incoming:
        camera["auto_memory_profile"] = bool(incoming["auto_memory_profile"])
    cfg.save()


def _detect_memory_tier_gb() -> int:
    try:
        mem_kb = int(Path("/proc/meminfo").read_text(encoding="utf-8").split()[1])
        mem_gb = mem_kb / (1024 * 1024)
        if mem_gb <= 2.5:
            return 2
        if mem_gb <= 5.0:
            return 4
        return 8
    except Exception:
        return 4


def apply_camera_overlay_host() -> tuple:
    script = project_path("setup_camera.sh")
    if not script.exists():
        return "", FileNotFoundError("setup_camera.sh not found")
    result = subprocess.run(
        ["sudo", "-n", "bash", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(project_path()),
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return output, RuntimeError(result.stderr or result.stdout or "overlay apply failed")
    return output, None
