import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, redirect, request, send_from_directory

from metrics import global_metrics
from paths import module_4g_path, project_path
from config import Config
from web.mavlink_bridge import bridge
import network_controller
from web.network_helpers import read_network_status
from web import camera_handlers, landing_handlers
from web import network_mode
from telemetry import global_telemetry
from logging_setup import configure_quiet_werkzeug

logger = logging.getLogger("WebServer")

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
START_TIME = datetime.now(timezone.utc)

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
_fwd_ref = None
_auth_ref = None
_cfg_ref = None
_module_lock = threading.Lock()


def _set_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


def _json_response(data, status=200):
    resp = jsonify(data)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-cache"
    return _set_cors(resp)


_SILENT_API_PATHS = frozenset({"/api/status", "/api/network/status"})


def _api_route(rule, methods=None):
    methods = methods or ["GET"]

    def decorator(func):
        @app.route(rule, methods=methods + ["OPTIONS"])
        @wraps(func)
        def wrapper(*args, **kwargs):
            if request.method == "OPTIONS":
                return _set_cors(app.make_response(("", 204)))
            start = time.time()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                logger.error("[WEB] %s %s failed: %s", request.method, request.path, exc)
                result = ({"success": False, "message": str(exc)}, 500)
            if isinstance(result, tuple):
                data, status = result[0], result[1] if len(result) > 1 else 200
                resp = _json_response(data, status)
            else:
                resp = _json_response(result)
            if request.path not in _SILENT_API_PATHS:
                logger.info(
                    "[WEB][REQ] %s %s -> %d (%dms)",
                    request.method,
                    request.path,
                    resp.status_code,
                    int((time.time() - start) * 1000),
                )
            else:
                logger.debug(
                    "[WEB][REQ] %s %s -> %d (%dms)",
                    request.method,
                    request.path,
                    resp.status_code,
                    int((time.time() - start) * 1000),
                )
            return resp

        return wrapper

    return decorator


def _build_metrics_snapshot() -> dict:
    snapshot = global_metrics.get_snapshot()
    telem = global_telemetry.snapshot()
    snapshot["telemetry_valid"] = telem.get("valid", False)
    snapshot["pixhawk_connected"] = telem.get("connected", False)
    snapshot["telemetry"] = telem
    if _fwd_ref and hasattr(_fwd_ref, "stats_lock"):
        with _fwd_ref.stats_lock:
            snapshot["stats"] = dict(_fwd_ref.stats)
    return snapshot


def _format_unix_timestamp(ts) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _project_path(*parts: str) -> str:
    return str(project_path(*parts))


def _read_network_status() -> dict:
    return read_network_status()


@app.route("/")
def root():
    return redirect("/dashboard.html")


@app.route("/templates/<path:template_name>")
def serve_template(template_name):
    if ".." in template_name:
        return _json_response({"error": "Invalid template name"}, 400)
    try:
        path = landing_handlers.template_file_path(template_name)
    except ValueError:
        return _json_response({"error": "Invalid template name"}, 400)
    if not path.is_file():
        return _json_response({"error": "not found"}, 404)
    resp = send_from_directory(str(path.parent), path.name, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return _set_cors(resp)


@app.route("/<path:filename>")
def static_files(filename):
    if filename.startswith("api/"):
        return _json_response({"error": "not found"}, 404)
    resp = send_from_directory(str(STATIC_DIR), filename)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return _set_cors(resp)


@_api_route("/api/status")
def api_status():
    return _build_metrics_snapshot()


@_api_route("/api/connection")
def api_connection():
    connected = bridge.is_connected()
    sys_id = bridge.get_system_id()
    active_path, ethernet_ok, serial_ok = bridge.get_mavlink_path()
    if connected:
        if active_path:
            message = f"Connected to Pixhawk (System ID: {sys_id}, path: {active_path})"
        else:
            message = f"Connected to Pixhawk (System ID: {sys_id})"
    else:
        message = "Waiting for Pixhawk connection..."
    return {
        "connected": connected,
        "systemId": sys_id if connected else 0,
        "message": message,
        "activePath": active_path,
        "ethernetOk": ethernet_ok,
        "serialOk": serial_ok,
    }


@_api_route("/api/health")
def api_health():
    return {"status": "ok"}


@_api_route("/api/param/set", methods=["POST"])
def api_param_set():
    data = request.get_json(silent=True) or {}
    result = bridge.set_parameter(
        data.get("paramName", ""),
        float(data.get("paramValue", 0)),
        data.get("paramType", "INT32"),
    )
    return result, 200 if result.get("success") else 400


@_api_route("/api/param/request-list", methods=["POST"])
def api_param_request_list():
    ok, message = bridge.request_parameter_list()
    return {"success": ok, "message": message}, 200 if ok else 400


@_api_route("/api/param/status")
def api_param_status():
    include = request.args.get("include") == "params"
    return bridge.get_parameter_list_status(include_params=include)


@_api_route("/api/param/list")
def api_param_list():
    status = bridge.get_parameter_list_status(include_params=True)
    return status.get("parameters") or []


@_api_route("/api/param/get")
def api_param_get():
    name = request.args.get("name", "")
    if not name:
        return {"error": "Missing 'name' parameter"}, 400
    param, found = bridge.get_cached_parameter(name)
    if not found:
        return {"found": False}
    return {"found": True, "param": param}


@_api_route("/api/v1/drone/api-key/status")
def api_key_status():
    if _auth_ref is None:
        return {"error": "Auth client not initialized"}, 503
    getter = getattr(_auth_ref, "get_api_key_status", None)
    if not getter:
        return {
            "has_active_key": False,
            "status": "none",
            "api_key": None,
            "error": "API key management not implemented in Python client",
        }
    try:
        state = _auth_ref.get_api_key_status()
        api_key = state.get("api_key")
        if not api_key and getattr(_auth_ref, "api_key", ""):
            api_key = _auth_ref.api_key
        return {
            "has_active_key": bool(state.get("has_active_key") or api_key),
            "status": state.get("status", "none"),
            "api_key": api_key,
            "created_at": _format_unix_timestamp(state.get("created_at")),
            "expires_at": _format_unix_timestamp(state.get("expires_at")),
            "user_uuid": state.get("user_uuid"),
            "username": None,
            "user_active_at": _format_unix_timestamp(state.get("user_active_at")),
        }
    except Exception as exc:
        return {
            "has_active_key": False,
            "status": "none",
            "api_key": None,
            "error": str(exc),
        }


@_api_route("/api/v1/drone/api-key/sync", methods=["POST"])
def api_key_sync():
    """Đồng bộ CLIENT API KEY đã được fleet server cấp (không tạo key mới)."""
    if _auth_ref is None:
        return {"error": "Auth client not initialized"}, 503
    syncer = getattr(_auth_ref, "sync_api_key_from_server", None)
    if not syncer:
        return {"error": "API key sync not implemented"}, 501
    try:
        state = syncer()
        api_key = state.get("api_key") or getattr(_auth_ref, "api_key", "")
        if not api_key:
            msg = "Server chưa trả API key"
            if state.get("status") == "backend_error":
                msg = "Fleet backend chưa sẵn sàng — lấy CLIENT API KEY từ QCloud Admin UI"
            return {"error": msg, "status": state.get("status", "none")}, 503
        return {
            "api_key": api_key,
            "status": state.get("status", "active"),
            "expires_at": _format_unix_timestamp(state.get("expires_at")),
            "message": "API key synced from fleet server",
        }
    except Exception as exc:
        return {"error": str(exc)}, 500


@_api_route("/api/v1/drone/api-key/request", methods=["POST"])
def api_key_request():
    if _auth_ref is None:
        return {"error": "Auth client not initialized"}, 503
    getter = getattr(_auth_ref, "request_api_key", None)
    if not getter:
        return {"error": "API key management not implemented in Python client"}, 501
    body = request.get_json(silent=True) or {}
    hours = int(body.get("expiration_hours", 24))
    hours = max(1, min(720, hours))
    try:
        state = _auth_ref.request_api_key(hours)
        return {
            "api_key": state.get("api_key"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": _format_unix_timestamp(state.get("expires_at")),
            "user_uuid": None,
            "username": None,
            "user_active_at": None,
        }
    except Exception as exc:
        status = 409 if "active API key" in str(exc) else 500
        return {"error": str(exc)}, status


@_api_route("/api/v1/drone/api-key/revoke", methods=["DELETE"])
def api_key_revoke():
    if _auth_ref is None:
        return {"error": "Auth client not initialized"}, 503
    revoker = getattr(_auth_ref, "revoke_api_key", None)
    if not revoker:
        return {"error": "API key management not implemented in Python client"}, 501
    try:
        revoker()
        return {"message": "API key revoked successfully"}
    except Exception as exc:
        return {"error": str(exc)}, 500


@_api_route("/api/v1/drone/api-key/delete", methods=["DELETE"])
def api_key_delete():
    if _auth_ref is None:
        return {"error": "Auth client not initialized"}, 503
    deleter = getattr(_auth_ref, "delete_api_key", None)
    if not deleter:
        return {"error": "API key management not implemented in Python client"}, 501
    try:
        deleter()
        return {"message": "API key deleted successfully"}
    except Exception as exc:
        return {"error": str(exc)}, 500


@_api_route("/api/telemetry")
def api_telemetry():
    return global_telemetry.snapshot()


@_api_route("/api/network/status")
def api_network_status():
    return _read_network_status()


@_api_route("/api/network/priority", methods=["GET", "POST"])
def api_network_priority():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        priority = body.get("priority", "")
        if priority not in ("4g", "wifi"):
            return {"success": False, "message": "Invalid priority. Use: 4g or wifi"}, 400
        try:
            with _module_lock:
                network_controller.set_priority(priority)
            threading.Thread(target=network_controller.run_once, daemon=True).start()
            return {"success": True, "message": f"Priority set to {priority}", "priority": priority}
        except FileNotFoundError:
            return {"success": False, "message": "4G module not available (WiFi-only build)"}, 503

    return {"success": True, "priority": network_controller.get_priority()}


@_api_route("/api/network/mode", methods=["GET", "POST"])
def api_network_mode():
    if _cfg_ref is None:
        return {"success": False, "message": "Config not loaded"}, 500
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        mode = str(body.get("mode", "")).strip()
        if not mode and "priority" in body:
            mode = network_mode.legacy_priority_to_mode(body.get("priority", ""))
        cloud_fallback = body.get("cloud_wifi_fallback")
        fallback_delay = body.get("fallback_delay")
        try:
            network_mode.apply_network_mode(
                _cfg_ref,
                mode,
                cloud_fallback=cloud_fallback if cloud_fallback is not None else None,
                fallback_delay=int(fallback_delay) if fallback_delay is not None else None,
            )
            return network_mode.build_network_mode_payload(_cfg_ref)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}, 400
        except FileNotFoundError:
            return {"success": False, "message": "4G module not available (WiFi-only build)"}, 503
    return network_mode.build_network_mode_payload(_cfg_ref)


@_api_route("/api/network/reconnect", methods=["POST"])
def api_network_reconnect():
    try:
        threading.Thread(target=network_controller.run_once, daemon=True).start()
        return {"success": True, "message": "Reconnection triggered"}
    except FileNotFoundError:
        return {"success": False, "message": "4G module not available (WiFi-only build)"}, 503


@_api_route("/api/network/switch", methods=["POST"])
def api_network_switch():
    body = request.get_json(silent=True) or {}
    target = str(body.get("target", "")).lower().strip()
    if target not in ("4g", "wifi"):
        return {"success": False, "message": "Invalid target. Use: 4g or wifi"}, 400

    def _switch():
        try:
            with _module_lock:
                network_controller.set_priority(target)
            time.sleep(0.3)
            network_controller.run_once()
        except Exception as exc:
            logger.error("[WEB][NET] Failed to switch network to %s: %s", target, exc)

    try:
        threading.Thread(target=_switch, daemon=True).start()
        return {"success": True, "message": "Network switch triggered", "target": target}
    except FileNotFoundError:
        return {"success": False, "message": "4G module not available (WiFi-only build)"}, 503


@_api_route("/api/network/test")
def api_network_test():
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "3", "8.8.8.8"],
            capture_output=True,
            text=True,
            timeout=6,
        )
        if result.returncode != 0:
            return {"success": False, "message": "No internet connectivity"}
        match = re.search(r"time[=<]([0-9.]+)\s*ms", result.stdout)
        latency = match.group(1) if match else "N/A"
        return {"success": True, "latency": latency, "message": "Connection test successful"}
    except Exception:
        return {"success": False, "message": "No internet connectivity"}


@_api_route("/api/network/4g/mode")
def api_4g_mode_get():
    script = module_4g_path("set_4g_mode.py")
    if not script.exists():
        return {"success": False, "message": "4G module not available (WiFi-only build)"}, 503
    try:
        with _module_lock:
            result = subprocess.run(
                ["python3", str(script), "get"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(PROJECT_ROOT),
            )
        return json.loads(result.stdout or "{}"), 200
    except Exception:
        return {"success": False, "message": "Failed to get 4G mode"}, 500


@_api_route("/api/network/4g/mode/set", methods=["POST"])
def api_4g_mode_set():
    script = module_4g_path("set_4g_mode.py")
    if not script.exists():
        return {"success": False, "message": "4G module not available (WiFi-only build)"}, 503

    body = request.get_json(silent=True) or {}
    mode_val = body.get("mode")
    if mode_val is None:
        return {"success": False, "message": "Mode parameter required"}, 400

    if isinstance(mode_val, str) and not mode_val.isdigit():
        return {"success": False, "message": f"Invalid mode: {mode_val}. Use numeric values: 2, 13, 14, 38, 51, 71"}, 400

    mode_str = str(int(mode_val)) if not isinstance(mode_val, str) or mode_val.isdigit() else mode_val
    try:
        with _module_lock:
            result = subprocess.run(
                ["python3", str(script), "set", mode_str],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(PROJECT_ROOT),
            )
        data = json.loads(result.stdout or "{}")
        status = 200 if data.get("success", True) else 400
        return data, status
    except Exception:
        return {"success": False, "message": "Failed to parse response from module"}, 500


@_api_route("/api/config/get")
def api_config_get():
    try:
        cfg = Config(_cfg_ref.filename if _cfg_ref else "config.yaml")
    except Exception as exc:
        return {"success": False, "message": f"Failed to load config: {exc}"}, 500
    return {"success": True, "config": cfg.data}


@_api_route("/api/config/network/update", methods=["POST"])
def api_config_network_update():
    if _cfg_ref is None:
        return {"success": False, "message": "Config not loaded"}, 500

    body = request.get_json(silent=True) or {}
    network = _cfg_ref.data.setdefault("network", {})
    mavlink = _cfg_ref.data.setdefault("mavlink", {})

    if "connection_type" in body:
        network["connection_type"] = body["connection_type"]
        mavlink["connection_type"] = body["connection_type"]
    if "serial_port" in body:
        network["serial_port"] = body["serial_port"]
        mavlink["serial_port"] = body["serial_port"]
    if "serial_baud" in body:
        network["serial_baud"] = int(body["serial_baud"])
        mavlink["serial_baud"] = int(body["serial_baud"])
    if "local_listen_port" in body:
        network["local_listen_port"] = int(body["local_listen_port"])
        network["tcp_port"] = int(body["local_listen_port"])
        mavlink["tcp_port"] = int(body["local_listen_port"])
    elif "tcp_port" in body:
        network["tcp_port"] = int(body["tcp_port"])
        network["local_listen_port"] = int(body["tcp_port"])
        mavlink["tcp_port"] = int(body["tcp_port"])
    if "target_host" in body:
        network["target_host"] = body["target_host"]
        _cfg_ref.data.setdefault("forwarding", {})["target_host"] = body["target_host"]
    if "target_port" in body:
        network["target_port"] = int(body["target_port"])
        _cfg_ref.data.setdefault("forwarding", {})["target_port"] = int(body["target_port"])

    try:
        _cfg_ref.save()
    except Exception as exc:
        return {"success": False, "message": f"Failed to save config: {exc}"}, 500

    return {
        "success": True,
        "message": "Network config updated successfully. Please restart the service for changes to take effect.",
        "config": _cfg_ref.get_network_config_for_api(),
    }


@_api_route("/api/config/hardware/update", methods=["POST"])
def api_config_hardware_update():
    if _cfg_ref is None:
        return {"success": False, "message": "Config not loaded"}, 500

    body = request.get_json(silent=True) or {}
    network = _cfg_ref.data.setdefault("network", {})
    mavlink = _cfg_ref.data.setdefault("mavlink", {})
    ethernet = _cfg_ref.data.setdefault("ethernet", {})
    lcd = _cfg_ref.data.setdefault("lcd", {})

    net_body = body.get("network", body)
    for key in ("connection_type", "serial_port"):
        if key in net_body:
            network[key] = net_body[key]
            mavlink[key] = net_body[key]
    if "serial_baud" in net_body:
        network["serial_baud"] = int(net_body["serial_baud"])
        mavlink["serial_baud"] = int(net_body["serial_baud"])
    if "local_listen_port" in net_body:
        network["local_listen_port"] = int(net_body["local_listen_port"])
        network["tcp_port"] = int(net_body["local_listen_port"])
        mavlink["tcp_port"] = int(net_body["local_listen_port"])

    eth_body = body.get("ethernet", body)
    for key, cast in (
        ("interface", str),
        ("local_ip", str),
        ("broadcast_ip", str),
        ("pixhawk_ip", str),
        ("subnet", str),
    ):
        if key in eth_body:
            ethernet[key] = cast(eth_body[key])
    for key in ("pixhawk_port", "pixhawk_connection_timeout"):
        if key in eth_body:
            ethernet[key] = int(eth_body[key])
    for key in ("auto_setup", "allow_missing_pixhawk"):
        if key in eth_body:
            ethernet[key] = bool(eth_body[key])

    lcd_body = body.get("lcd", body)
    for key in ("enabled", "auto_setup"):
        if key in lcd_body:
            lcd[key] = bool(lcd_body[key])
    for key in ("overlay", "pins", "screen"):
        if key in lcd_body:
            lcd[key] = str(lcd_body[key])
    for key in ("bus", "address", "interval", "screen_hold"):
        if key in lcd_body:
            lcd[key] = int(lcd_body[key])

    try:
        _cfg_ref.save()
    except Exception as exc:
        return {"success": False, "message": f"Failed to save config: {exc}"}, 500

    return {
        "success": True,
        "message": "Hardware config updated. Restart service for MAVLink/LCD changes.",
        "config": _cfg_ref.data,
    }


@_api_route("/api/camera/detect")
def api_camera_detect():
    refresh = request.args.get("refresh") == "1"
    data, status = camera_handlers.camera_detect(refresh=refresh)
    return data, status


@_api_route("/api/camera/registry")
def api_camera_registry():
    data, status = camera_handlers.camera_registry()
    return data, status


@_api_route("/api/camera/ports", methods=["POST"])
def api_camera_ports():
    body = request.get_json(silent=True) or {}
    data, status = camera_handlers.camera_ports_save(body.get("ports") or {})
    return data, status


@_api_route("/api/camera/restart", methods=["POST"])
def api_camera_restart():
    data, status = camera_handlers.camera_restart()
    return data, status


@_api_route("/api/camera/start", methods=["POST"])
def api_camera_start():
    data, status = camera_handlers.camera_start()
    return data, status


@_api_route("/api/camera/stop", methods=["POST"])
def api_camera_stop():
    data, status = camera_handlers.camera_stop()
    return data, status


@_api_route("/api/camera/status")
def api_camera_status():
    return camera_handlers.camera_status()


@_api_route("/api/camera/landing")
def api_camera_landing():
    max_age = float(request.args.get("max_age_sec", 10) or 10)
    camera_id = request.args.get("camera_id")
    if camera_id is not None and str(camera_id).strip() != "":
        cam_id = int(camera_id)
        entry = {"camera_id": cam_id}
        from web.camera_service import read_landing_telemetry, read_stream_stats

        landing = read_landing_telemetry(cam_id, max_age)
        if landing:
            entry["landing"] = landing
        stats = read_stream_stats(cam_id, max_age)
        if stats:
            entry["stream_stats"] = stats
        return {"success": True, "cameras": [entry]}
    from web.camera_service import landing_status_for_config

    return {"success": True, "cameras": landing_status_for_config(_cfg_ref, max_age)}


@_api_route("/api/camera/apply-overlay", methods=["POST"])
def api_camera_apply_overlay():
    from web.camera_service import apply_camera_overlay_host

    output, err = apply_camera_overlay_host()
    if err:
        return {
            "success": False,
            "message": "Không thể áp dụng overlay/reboot. Chạy: sudo bash setup_camera.sh",
            "error": str(err),
            "output": output,
        }, 500
    return {
        "success": True,
        "message": "Đã áp dụng boot overlay — reboot trong ~2s nếu config đổi",
        "output": output,
    }


@_api_route("/api/camera/config/global", methods=["GET", "POST"])
def api_camera_config_global():
    from web.camera_service import camera_global_payload, save_camera_global_from_ui

    if _cfg_ref is None:
        return {"success": False, "message": "Config not loaded"}, 500
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            save_camera_global_from_ui(_cfg_ref, body)
        except Exception as exc:
            return {"success": False, "message": str(exc)}, 500
    return camera_global_payload(_cfg_ref)


@_api_route("/api/camera/test")
def api_camera_test():
    data, status = camera_handlers.camera_test()
    return data, status


@_api_route("/api/camera/config/load")
def api_camera_config_load():
    camera_id = int(request.args.get("camera_id", 0))
    data, status = camera_handlers.camera_config_load(_cfg_ref, camera_id)
    return data, status


@_api_route("/api/camera/config/save", methods=["POST"])
def api_camera_config_save():
    body = request.get_json(silent=True) or {}
    data, status = camera_handlers.camera_config_save(body, _cfg_ref)
    return data, status


@_api_route("/api/landing/templates")
def api_landing_templates():
    data, status = landing_handlers.list_templates()
    return data, status


@_api_route("/api/landing/config/load")
def api_landing_config_load():
    data, status = landing_handlers.landing_config_load()
    return data, status


@_api_route("/api/landing/config/save", methods=["POST"])
def api_landing_config_save():
    body = request.get_json(silent=True) or {}
    data, status = landing_handlers.landing_config_save(body)
    return data, status


@_api_route("/api/landing/templates/upload", methods=["POST"])
def api_landing_templates_upload():
    if "file" not in request.files:
        return {"success": False, "message": "No file uploaded"}, 400
    upload = request.files["file"]
    if not upload.filename:
        return {"success": False, "message": "Empty filename"}, 400
    data, status = landing_handlers.upload_template(upload.filename, upload.read())
    return data, status


def start_server(port: int, stats, auth, forwarder=None, cfg=None):
    global _fwd_ref, _auth_ref, _cfg_ref
    _fwd_ref = forwarder or stats
    _auth_ref = auth
    _cfg_ref = cfg

    if forwarder and getattr(forwarder, "get_active_connection", None):
        conn = forwarder.get_active_connection()
        if conn is not None:
            bridge.set_connection(conn)

    configure_quiet_werkzeug()
    app.logger.handlers.clear()
    app.logger.propagate = True
    logger.info("Starting web server on http://0.0.0.0:%d", port)

    def _run_web():
        import werkzeug.serving

        werkzeug.serving.run_simple(
            "0.0.0.0",
            port,
            app,
            threaded=True,
            use_reloader=False,
            use_debugger=False,
        )

    threading.Thread(target=_run_web, daemon=True, name="web-server").start()
