#!/usr/bin/env python3
"""
Auto Enable 4G và cấp phát IP cho wwan0
Sử dụng QMI (qmicli) để quản lý module SIM7600
Driver: qmi_wwan + cdc_wdm  →  /dev/cdc-wdm0
"""

import subprocess
import time
import re
import atexit
import os
import json
import gpiod
from gpiod.line import Direction, Value
import serial
import serial.tools.list_ports

IS_ROOT = False

# ─── Cấu hình ──────────────────────────────────────────────────
QMI_DEV    = "/dev/cdc-wdm0"
IFACE      = os.getenv("DRONEBRIDGE_WWAN_IFACE", "wwan0")
MODEM_STATE_FILE = "/run/dronebridge/modem_net.json"
APN        = "v-internet"          # APN Viettel
SIM7600_VID = "1e0e"
SIM7600_PID_ALLOW = {
    p.strip().lower() for p in os.getenv("SIM7600_PID_ALLOW", "").split(",") if p.strip()
}
IFACE_SEARCH_TIMEOUT_S = int(os.getenv("SIM7600_IFACE_SEARCH_TIMEOUT_S", "45"))
QMI_RETRY_ERRORS = (
    "CID allocation failed",
    "Service mismatch",
    "endpoint hangup",
    "Couldn't create client",
    "Transaction timed out",
    "Resource temporarily unavailable",
    "Unexpected response of type",
    "Error reading from istream",
)

# GPIO pins (gpiochip0)
GPIOCHIP           = "gpiochip0"
GPIO_POWER_MAIN    = 27   # LOW  = Power ON  (đảo logic)
GPIO_CM5_ON_OFF_4G = 10   # LOW  = Power ON  (transistor đảo logic)
GPIO_CM5_RESET_4G  = 17   # LOW  = Normal (đổi từ GPIO22 — nhường I2C3 LCD)
GPIO_W_DISABLE1    = 2    # HIGH = RF Enabled
GPIO_W_DISABLE2    = 3    # HIGH = GNSS Enabled
GPIO_PINS = [GPIO_POWER_MAIN, GPIO_CM5_ON_OFF_4G, GPIO_CM5_RESET_4G,
             GPIO_W_DISABLE1, GPIO_W_DISABLE2]

# ─── GPIO (libgpiod v2 - giữ state suốt vòng đời script) ──────
_gpio_req = None

def _gpio_release():
    global _gpio_req
    if _gpio_req:
        try: _gpio_req.release()
        except Exception: pass
        _gpio_req = None

atexit.register(_gpio_release)

def _gpio_init():
    global _gpio_req
    if _gpio_req:
        return True
    try:
        cfg = {p: gpiod.LineSettings(direction=Direction.OUTPUT,
                                     output_value=Value.INACTIVE)
               for p in GPIO_PINS}
        _gpio_req = gpiod.request_lines(f"/dev/{GPIOCHIP}",
                                        consumer="enable_4g", config=cfg)
        return True
    except Exception as e:
        print(f"  ! GPIO init lỗi: {e}")
        return False

def gpio_set(pin, val):
    if not _gpio_init(): return False
    try:
        _gpio_req.set_value(pin, Value.ACTIVE if val else Value.INACTIVE)
        return True
    except Exception as e:
        print(f"  ! GPIO{pin}={val}: {e}")
        return False

def gpio_get(pin):
    if not _gpio_init(): return None
    try:
        return 1 if _gpio_req.get_value(pin) == Value.ACTIVE else 0
    except Exception:
        return None

# ─── Helpers ───────────────────────────────────────────────────
def run(cmd, timeout=10, check=False):
    """Chạy lệnh shell, trả về (returncode, stdout, stderr)"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"

def qmicli_raw(args, timeout=15):
    """Gọi qmicli 1 lần với device cố định, trả về (rc, out, err)."""
    cmd = ["qmicli", "-d", QMI_DEV] + args
    if not IS_ROOT:
        cmd = ["sudo"] + cmd
    return run(cmd, timeout=timeout)

def run_root(cmd, timeout=10):
    """Run command with root privileges when needed."""
    if IS_ROOT:
        return run(cmd, timeout=timeout)
    return run(["sudo"] + cmd, timeout=timeout)


# ─── wwan0 netdev (khối 4G — chỉ file này rename / ghi modem_net.json) ─────
def _discover_modem_netdev():
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return None
    for name in names:
        if not (name.startswith("wwu") or name.startswith("wwan")):
            continue
        dev_path = f"/sys/class/net/{name}/device"
        if not os.path.exists(dev_path):
            continue
        try:
            usb_dev = os.path.basename(os.path.realpath(dev_path)).split(":")[0]
            with open(f"/sys/bus/usb/devices/{usb_dev}/idVendor") as f:
                if f.read().strip().lower() != SIM7600_VID:
                    continue
        except OSError:
            pass
        return name
    return None


def ensure_canonical_wwan():
    if os.path.exists(f"/sys/class/net/{IFACE}"):
        return True
    src = _discover_modem_netdev()
    if not src or src == IFACE:
        return bool(src)
    run_root(["ip", "link", "set", src, "down"], timeout=5)
    rc, _, _ = run_root(["ip", "link", "set", src, "name", IFACE], timeout=5)
    return rc == 0 and os.path.exists(f"/sys/class/net/{IFACE}")


def _get_wwan_ipv4():
    rc, out, _ = run(["ip", "-4", "addr", "show", IFACE])
    if rc != 0 or not out:
        return None
    if "state UP" not in out and "state UNKNOWN" not in out:
        if ",UP>" not in out and "<UP," not in out:
            return None
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def _write_modem_state(ready: bool, ip=None, phase: str = ""):
    os.makedirs(os.path.dirname(MODEM_STATE_FILE), exist_ok=True)
    with open(MODEM_STATE_FILE, "w") as f:
        json.dump({
            "iface": IFACE, "ip": ip, "ready": ready, "phase": phase,
            "qmi_dev": QMI_DEV if os.path.exists(QMI_DEV) else None,
        }, f, indent=2)


def wait_qmi_ready(max_wait=12):
    """Đợi endpoint QMI phản hồi ổn định trước khi gọi service cụ thể."""
    deadline = time.time() + max_wait
    last_err = ""
    while time.time() < deadline:
        rc, out, err = qmicli_raw(["--dms-get-manufacturer"], timeout=5)
        if rc == 0 and "Manufacturer" in out:
            return True
        last_err = (err or out or "").strip()
        time.sleep(1)
    warn(f"QMI chưa sẵn sàng sau {max_wait}s: {last_err or 'unknown error'}")
    return False

def qmicli(args, timeout=15, retries=4):
    """Gọi qmicli với retry cho lỗi transient (CID/service mismatch)."""
    last_err = ""
    retry_tokens = tuple(t.lower() for t in QMI_RETRY_ERRORS)
    for attempt in range(1, retries + 1):
        rc, out, err = qmicli_raw(args, timeout=timeout)
        if rc == 0:
            return out.strip(), None

        last_err = (err or out or "").strip()
        low_err = last_err.lower()
        transient = any(token in low_err for token in retry_tokens)
        if not transient:
            return None, last_err

        if attempt < retries:
            warn(f"QMI transient error (lần {attempt}/{retries}): {last_err}")
            # Đồng bộ lại CTL và chờ endpoint ổn định trước khi retry
            qmicli_raw(["--ctl-sync"], timeout=6)
            wait_qmi_ready(max_wait=8)
            time.sleep(min(1 + attempt, 4))

    return None, last_err

def find_at_port():
    """Find AT command serial port for SIM7600."""
    preferred = ["/dev/ttyUSB2", "/dev/ttyUSB1", "/dev/ttyUSB0"]
    for p in preferred:
        try:
            with serial.Serial(p, 115200, timeout=1) as ser:
                ser.write(b"AT\r\n")
                time.sleep(0.2)
                resp = ser.read(ser.in_waiting or 1).decode(errors="ignore")
                if "OK" in resp:
                    return p
        except Exception:
            continue

    for p in serial.tools.list_ports.comports():
        dev = p.device
        if "ttyUSB" not in dev:
            continue
        try:
            with serial.Serial(dev, 115200, timeout=1) as ser:
                ser.write(b"AT\r\n")
                time.sleep(0.2)
                resp = ser.read(ser.in_waiting or 1).decode(errors="ignore")
                if "OK" in resp:
                    return dev
        except Exception:
            continue

    return None

def send_at(command, timeout=3):
    """Send AT command and return response text."""
    port = find_at_port()
    if not port:
        return None, "khong tim thay cong AT"

    try:
        with serial.Serial(port, 115200, timeout=timeout, rtscts=False, dsrdtr=False) as ser:
            ser.setDTR(False)
            ser.setRTS(False)
            time.sleep(0.2)
            ser.reset_input_buffer()

            ser.write((command.strip() + "\r\n").encode())
            ser.flush()

            lines = []
            start = time.time()
            while time.time() - start < timeout:
                if ser.in_waiting > 0:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if line:
                        lines.append(line)
                    if "OK" in line or "ERROR" in line or "CME ERROR" in line:
                        break
                time.sleep(0.05)

        text = "\n".join(lines)
        if "ERROR" in text or "CME ERROR" in text:
            return text, text
        return text, None
    except Exception as e:
        return None, str(e)

def at_recover_registration():
    """Recover registration by forcing auto operator selection and mode."""
    info("AT recovery: deregister + auto operator selection...")

    # Deregister from current network then return to automatic mode.
    send_at("AT+COPS=2", timeout=8)
    time.sleep(2)

    # Prefer full automatic selection first.
    send_at("AT+CNMP=2", timeout=5)
    time.sleep(1)

    # Trigger automatic network selection.
    _, err = send_at("AT+COPS=0", timeout=20)
    if err:
        warn(f"AT+COPS=0 lỗi: {err}")
        return False

    ok("AT recovery đã gửi thành công")
    return True

def step_prepare_qmi_environment():
    """Stop competing modem daemons/processes to prevent CID exhaustion."""
    section("CHUẨN BỊ — Dọn môi trường QMI")

    # ModemManager commonly grabs QMI clients and causes ClientIdsExhausted.
    rc, _, _ = run_root(["systemctl", "is-active", "ModemManager"], timeout=4)
    if rc == 0:
        info("Dừng ModemManager để tránh tranh chấp QMI client...")
        run_root(["systemctl", "stop", "ModemManager"], timeout=8)
        ok("ModemManager đã dừng")
    else:
        info("ModemManager không chạy")

    # Cleanup stale qmi tools from previous runs.
    run_root(["pkill", "-f", "qmicli"], timeout=3)
    run_root(["pkill", "-f", "qmi-network"], timeout=3)
    run_root(["pkill", "-f", "uqmi"], timeout=3)
    ok("Đã dọn process QMI cũ")

def section(title):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")

def ok(msg):  print(f"  ✓ {msg}")
def fail(msg): print(f"  ✗ {msg}")
def info(msg): print(f"  • {msg}")
def warn(msg): print(f"  ! {msg}")

# ─── BƯỚC 1: GPIO Power ON ────────────────────────────────────
def step_gpio_power_on():
    section("BƯỚC 1/6 — GPIO Power ON")
    import os

    # Check trạng thái USB trước khi thay đổi GPIO
    already_up = os.path.exists("/dev/ttyUSB2")

    gpio_map = [
        (GPIO_POWER_MAIN,    0, "PWR_MAIN    → ON  (low)"),
        (GPIO_CM5_ON_OFF_4G, 0, "ON_OFF_4G   → Power ON  (low)"),
        (GPIO_CM5_RESET_4G,  0, "RESET_4G    → Normal    (low)"),
        (GPIO_W_DISABLE1,    1, "W_DISABLE1  → RF ON"),
        (GPIO_W_DISABLE2,    1, "W_DISABLE2  → GNSS ON"),
    ]
    for pin, val, desc in gpio_map:
        gpio_set(pin, val)
        actual = gpio_get(pin)
        status = "✓" if actual == val else "✗"
        print(f"  {status} GPIO{pin:2} = {actual}  ({desc})")

    if already_up:
        ok("Module đã sẵn sàng trên USB — bỏ qua boot wait")
    else:
        # Module vừa được cấp nguồn → phải đợi boot hoàn tất
        info("Module vừa được cấp nguồn — đợi boot (25s)...")
        for remaining in range(25, 0, -1):
            print(f"  {remaining:2}s...", end="\r")
            time.sleep(1)
            if remaining % 5 == 0 and os.path.exists("/dev/ttyUSB2"):
                print(f"  ✓ /dev/ttyUSB2 xuất hiện sớm ({25 - remaining + 1}s)    ")
                break
        else:
            print()
        ok("GPIO boot wait hoàn tất")

# ─── BƯỚC 2: Switch driver simcom_wwan → qmi_wwan ────────────
def _find_sim7600_usb_iface():
    """
    Tìm USB interface path của SIM7600 (network/NDIS endpoint) trong sysfs.
    Trả về string như '2-1:1.5' hoặc None.
    """
    import glob, os

    # CÁCH 1 (nhanh nhất): lấy qua symlink của wwan0 nếu interface đã tồn tại
    wwan0_dev = f"/sys/class/net/{IFACE}/device"
    if os.path.exists(wwan0_dev):
        iface = os.path.basename(os.path.realpath(wwan0_dev))
        if ":" in iface:   # tên dạng "2-1:1.5"
            return iface

    # CÁCH 2: tìm qua glob /sys/bus/usb/devices/
    best = None
    best_num = -1
    for uevent_path in glob.glob("/sys/bus/usb/devices/*/uevent"):
        try:
            content = open(uevent_path).read()
        except Exception:
            continue
        if "DEVTYPE=usb_interface" not in content:
            continue
        if not re.search(r"PRODUCT=" + re.escape(SIM7600_VID) + r"/[0-9a-fA-F]+/", content):
            continue
        name = uevent_path.replace("/uevent", "").split("/")[-1]
        if "INTERFACE=255/255/255" in content:
            try:
                num = int(name.split(".")[-1])
                if num > best_num:
                    best_num = num
                    best = name
            except Exception:
                best = name
    if best:
        return best

    # CÁCH 3: fallback theo idVendor/idProduct + bInterfaceClass/subclass/protocol
    for iface_dir in sorted(glob.glob("/sys/bus/usb/devices/*:*")):
        try:
            root = os.path.basename(iface_dir).split(":")[0]
            vid = open(f"/sys/bus/usb/devices/{root}/idVendor").read().strip().lower()
            pid = open(f"/sys/bus/usb/devices/{root}/idProduct").read().strip().lower()
            if vid != SIM7600_VID:
                continue
            if SIM7600_PID_ALLOW and pid not in SIM7600_PID_ALLOW:
                continue
            icls = open(f"{iface_dir}/bInterfaceClass").read().strip().lower()
            isub = open(f"{iface_dir}/bInterfaceSubClass").read().strip().lower()
            ipro = open(f"{iface_dir}/bInterfaceProtocol").read().strip().lower()
            if icls == "ff" and isub == "ff" and ipro == "ff":
                return os.path.basename(iface_dir)
        except Exception:
            continue

    return None


def _usb_reset_sim7600_device() -> bool:
    """Reset USB device khi modem chỉ còn node device, mất interface (2-1:1.x)."""
    import glob
    import os

    for dev_path in sorted(glob.glob("/sys/bus/usb/devices/*")):
        if ":" in os.path.basename(dev_path):
            continue
        try:
            vid_path = os.path.join(dev_path, "idVendor")
            auth_path = os.path.join(dev_path, "authorized")
            if not os.path.isfile(vid_path) or not os.path.isfile(auth_path):
                continue
            with open(vid_path) as f:
                vid = f.read().strip().lower()
            if vid != SIM7600_VID:
                continue
            dev_name = os.path.basename(dev_path)
            info(f"USB reset modem device {dev_name} (authorized 0→1)...")
            run_root(["sh", "-c", f"echo 0 > {auth_path}"])
            time.sleep(2)
            run_root(["sh", "-c", f"echo 1 > {auth_path}"])
            time.sleep(5)
            return True
        except Exception:
            continue
    return False


def step_switch_to_qmi_driver():
    section("BƯỚC 2/6 — Switch driver → qmi_wwan")
    import os

    # Đảm bảo drivers được load (nếu chưa có)
    rc_lsmod, lsmod_out, _ = run(["lsmod"])
    if "cdc_wdm" not in lsmod_out:
        run_root(["modprobe", "cdc_wdm"])
    if "qmi_wwan" not in lsmod_out:
        run_root(["modprobe", "qmi_wwan"])

    # Tìm USB interface của SIM7600 (PRODUCT=1e0e/9001, INTERFACE=255/255/255)
    usb_iface = None
    for _ in range(max(10, IFACE_SEARCH_TIMEOUT_S)):
        usb_iface = _find_sim7600_usb_iface()
        if usb_iface:
            break
        time.sleep(1)
    if not usb_iface:
        warn("Không thấy USB interface — thử USB reset rồi tìm lại...")
        if _usb_reset_sim7600_device():
            for _ in range(20):
                usb_iface = _find_sim7600_usb_iface()
                if usb_iface:
                    break
                time.sleep(1)
    if not usb_iface:
        fail(
            "Không tìm thấy USB interface của SIM7600 trong sysfs "
            f"(timeout={max(10, IFACE_SEARCH_TIMEOUT_S)}s, vid={SIM7600_VID})"
        )
        return False
    ok(f"USB interface: {usb_iface}")

    # Lấy driver hiện tại
    try:
        ue = open(f"/sys/bus/usb/devices/{usb_iface}/uevent").read()
        cur_drv = next((l.split("=")[1] for l in ue.splitlines() if l.startswith("DRIVER=")), None)
    except Exception:
        cur_drv = None
    info(f"Driver hiện tại: {cur_drv or 'none'}")

    # Luôn reload driver để reset QMI CID state (tránh ClientIdsExhausted)
    info("Reset USB + reload driver để clear QMI CID state...")

    # Unbind trước
    for drv in ("simcom_wwan", "qmi_wwan"):
        unbind = f"/sys/bus/usb/drivers/{drv}/unbind"
        if os.path.exists(unbind):
            run_root(["sh", "-c", f'echo "{usb_iface}" > {unbind} 2>/dev/null || true'])
    # Unload modules (qmi_wwan trước, rồi cdc_wdm)
    for _ in range(5):
        if not os.path.exists(QMI_DEV): break
        time.sleep(0.3)
    run_root(["rmmod", "qmi_wwan"], timeout=5)
    run_root(["rmmod", "cdc_wdm"], timeout=5)

    # USB device reset để clear CID trong firmware modem
    usb_dev = usb_iface.split(":")[0]   # "2-1:1.5" → "2-1"
    info(f"USB reset: /sys/bus/usb/devices/{usb_dev}/authorized 0→1...")
    run_root(["sh", "-c", f'echo 0 > /sys/bus/usb/devices/{usb_dev}/authorized'])
    time.sleep(2)
    run_root(["sh", "-c", f'echo 1 > /sys/bus/usb/devices/{usb_dev}/authorized'])
    time.sleep(3)   # Đợi USB re-enumerate

    # Load lại driver
    run_root(["modprobe", "cdc_wdm"])
    run_root(["modprobe", "qmi_wwan"])
    time.sleep(2)

    # Bind vào qmi_wwan
    info(f"Bind {usb_iface} → qmi_wwan...")
    run_root(["sh", "-c",
         f'echo "{usb_iface}" > /sys/bus/usb/drivers/qmi_wwan/bind 2>/dev/null || true'])

    # Đợi cdc-wdm0 xuất hiện
    appeared = False
    for i in range(20):
        if os.path.exists(QMI_DEV):
            ok(f"{QMI_DEV} xuất hiện sau {i+1}s")
            appeared = True
            break
        time.sleep(1)

    if not appeared:
        fail(f"{QMI_DEV} không xuất hiện sau 20s")
        return False

    # Probe cho đến khi QMI endpoint thực sự sẵn sàng (tối đa 30s)
    info("Đợi QMI endpoint sẵn sàng (probe qmicli)...")
    for attempt in range(3):
        if wait_qmi_ready(max_wait=10):
            ok(f"QMI sẵn sàng sau ~{attempt+1}s probe")
            if ensure_canonical_wwan():
                ok(f"Netdev QMI: {IFACE}")
            else:
                warn(f"Chưa có {IFACE} — udev hoặc rename thất bại")
            return True
        time.sleep(1)

    fail("QMI endpoint không phản hồi ổn định sau khi bind driver")
    return False

# ─── BƯỚC 3: Kiểm tra modem qua QMI ─────────────────────────
def step_verify_modem():
    section("BƯỚC 3/6 — Kiểm tra modem (DMS)")

    # Tránh lỗi CID allocation/service mismatch ngay sau USB re-enumeration
    wait_qmi_ready(max_wait=10)

    out, err = qmicli(["--dms-get-manufacturer"])
    if out is None:
        fail(f"Không kết nối được QMI: {err}")
        return False
    mfg = re.search(r"Manufacturer: '(.+)'", out)
    ok(f"Manufacturer: {mfg.group(1) if mfg else out.splitlines()[0]}")

    out, _ = qmicli(["--dms-get-model"])
    model = re.search(r"Model: '(.+)'", out or "")
    ok(f"Model: {model.group(1) if model else 'N/A'}")

    out, _ = qmicli(["--dms-get-revision"])
    rev = re.search(r"Revision: '(.+)'", out or "")
    ok(f"Revision: {rev.group(1) if rev else 'N/A'}")

    out, _ = qmicli(["--dms-get-ids"])
    imei = re.search(r"ESN: '(.+)'|IMEI: '(.+)'", out or "")
    ok(f"IMEI/ESN: {imei.group(0) if imei else 'N/A'}")

    def _sim_present_after_reprobe():
        card_out, _ = qmicli(["--uim-get-card-status"], timeout=10)
        return card_out, bool(card_out and "Card state: 'present'" in card_out)

    def _at_cpin_hint():
        resp, _ = send_at("AT+CPIN?", timeout=4)
        if not resp:
            return
        if "SIM not inserted" in resp or "NOT INSERTED" in resp.upper():
            info("AT+CPIN?: SIM chưa gắn trong khe — kiểm tra SIM nano, hướng chip, ép khe cho khít")
        elif "+CPIN:" in resp:
            info(f"AT+CPIN?: {resp.strip()}")
        elif "CME ERROR" in resp:
            info(f"AT+CPIN?: {resp.strip()}")

    def _recover_sim_no_atr(attempt=1, max_attempts=3):
        warn(f"SIM chưa sẵn sàng (no-ATR/absent) — reset RESET_4G (GPIO17) lần {attempt}/{max_attempts}")
        gpio_set(GPIO_CM5_RESET_4G, 1)
        time.sleep(1.5)
        gpio_set(GPIO_CM5_RESET_4G, 0)
        time.sleep(8)
        qmicli_raw(["--ctl-sync"], timeout=6)
        wait_qmi_ready(max_wait=12)

    # SIM must be present and unlocked before NAS registration can succeed.
    out, _ = _sim_present_after_reprobe()
    if out:
        if "Card state: 'present'" in out:
            ok("SIM card: present")
        else:
            card_state = "unknown"
            m = re.search(r"Card state: '([^']+)'", out)
            if m:
                card_state = m.group(1)
            warn(f"SIM card state: {card_state}")
            if "no-atr-received" in out or "absent" in out:
                present = False
                for attempt in range(1, 4):
                    _recover_sim_no_atr(attempt=attempt)
                    out, present = _sim_present_after_reprobe()
                    if present:
                        ok(f"SIM card: present (recovered sau reset lần {attempt})")
                        break
                if not present:
                    _at_cpin_hint()
                    fail("SIM card không present (no-ATR) — modem OK, cần gắn/lại SIM vật lý")
                    return False
            else:
                fail("SIM card không present")
                return False

        pin_needed = ("PIN1 state: 'enabled-not-verified'" in out or
                      "UPIN state: 'enabled-not-verified'" in out)
        if pin_needed:
            fail("SIM đang yêu cầu PIN (enabled-not-verified)")
            info("Hãy nhập PIN SIM trước hoặc tắt PIN lock")
            return False

    # Kiểm tra và bật online mode
    out, _ = qmicli(["--dms-get-operating-mode"])
    mode = re.search(r"Mode: '(.+)'", out or "")
    mode_str = mode.group(1) if mode else "unknown"
    info(f"Operating mode: {mode_str}")

    if mode_str != "online":
        info("Set operating mode → online...")
        out2, err2 = qmicli(["--dms-set-operating-mode=online"], timeout=10)
        if out2 is not None:
            ok("Operating mode = online")
        else:
            warn(f"Set mode lỗi: {err2}")

    return True

# ─── BƯỚC 3.5: Cấu hình radio modem qua AT ────────────────────
# Thực hiện sau khi QMI driver sẵn sàng, TRƯỚC khi đăng ký mạng.
# Các setting này được lưu vào NVM của modem → persist qua power cycle.
# Đây là bước quan trọng để ổn định kết nối LTE ở Việt Nam.
def step_configure_modem_at():
    section("BƯỚC 3.5/6 — Cấu hình radio modem (AT commands)")

    port = find_at_port()
    if not port:
        warn("Không tìm thấy AT port — bỏ qua cấu hình radio (kết nối vẫn tiếp tục)")
        return False

    ok(f"AT port: {port}")

    at_cmds = [
        # ── Tắt sleep / power-saving modes ──────────────────────────
        # Các mode này khiến modem tự "ngủ" khi không có data, gây ngắt kết nối bất ngờ.
        ("AT+CPSMS=0",  5, "Tắt PSM (Power Saving Mode)"),
        ("AT+CEDRXS=0", 5, "Tắt eDRX (Extended Discontinuous Reception)"),
        ("AT+CSCLK=0",  3, "Tắt slow clock / sleep mode"),

        # ── Chế độ mạng: Auto (LTE ưu tiên, fallback GSM/WCDMA) ─────
        # 38=LTE Only sẽ hoàn toàn mất kết nối khi sóng LTE yếu.
        # 2=Auto cho phép fallback xuống WCDMA/GSM → ổn định hơn.
        ("AT+CNMP=2",   5, "Chế độ mạng: Auto (LTE ưu tiên, có fallback)"),

        # ── Cấu hình LTE bands cho Việt Nam ─────────────────────────
        # Viettel/Mobifone/Vinaphone sử dụng Band 3 (1800MHz) làm band chính.
        # SIM7600 factory default KHÔNG bao gồm Band 3 trong scan list!
        # GSM bands : giữ nguyên default 0x0002000000400183
        # LTE bands : Enable Band 1(2100),2(1900),3(1800),7(2600),8(900),20(800),28(700)
        #             Bitmap: 0x00000000080800C7
        # TDS bands : giữ nguyên default 0x0000000000000021
        ("AT+CNBP=0x0002000000400183,0x00000000080800C7,0x0000000000000021",
         5, "LTE bands: 1,2,3(Viettel),7,8,20,28 enabled"),

        # ── APN mặc định (backup — QMI WDS sẽ override khi kết nối) ─
        ('AT+CGDCONT=1,"IP","v-internet"', 3, "APN: v-internet (Viettel)"),

        # ── Bật URC đăng ký mạng (giúp debug khi kiểm tra thủ công) ─
        ("AT+CREG=2",  2, "GSM registration URC với location info"),
        ("AT+CEREG=2", 2, "LTE registration URC với location info"),
    ]

    all_ok = True
    for cmd, timeout, desc in at_cmds:
        resp, err = send_at(cmd, timeout=timeout)
        if err and "OK" not in (resp or ""):
            warn(f"  AT '{cmd[:30]}' lỗi: {err} — bỏ qua (không critical)")
            all_ok = False
        else:
            ok(f"{desc}")

    if all_ok:
        ok("Cấu hình radio AT hoàn tất — LTE Band 3, PSM off, Auto mode")
    else:
        warn("Một số AT command không thành công — kết nối vẫn tiếp tục (non-fatal)")

    return True

# ─── BƯỚC 4: Đăng ký mạng (NAS) ──────────────────────────────
def step_wait_network():
    section("BƯỚC 4/6 — Đăng ký mạng (NAS, tối đa 120s)")
    start = time.time()
    last_sig_dbm = "??"
    consecutive_nas_fail = 0
    next_sig_query = 0
    deny_count = 0
    recovered_once = False
    at_recovered_once = False

    while (time.time() - start) < 120:
        elapsed = int(time.time() - start)

        # Signal strength: giảm tần suất query để tránh áp lực tạo NAS client liên tục
        if time.time() >= next_sig_query:
            sig_out, _ = qmicli(["--nas-get-signal-strength"], timeout=8)
            if sig_out:
                m = re.search(r"Network '(.+?)'.*?Current:\s*(-?\d+) dBm", sig_out, re.DOTALL)
                if m:
                    last_sig_dbm = f"{m.group(2)} dBm ({m.group(1)})"
                else:
                    m2 = re.search(r"Current:\s*(-?\d+) dBm", sig_out)
                    if m2:
                        last_sig_dbm = f"{m2.group(1)} dBm"
            next_sig_query = time.time() + 9

        # Registration status
        reg_out, reg_err = qmicli(["--nas-get-serving-system"], timeout=10, retries=3)
        reg_state = "Chưa đăng ký"
        rat = ""
        operator = ""
        if reg_out:
            consecutive_nas_fail = 0
            cs = re.search(r"Registration state: '(.+?)'", reg_out)
            if cs: reg_state = cs.group(1)
            r = re.search(r"Radio interfaces:.*?'(.+?)'", reg_out, re.DOTALL)
            if r: rat = r.group(1).upper()
            op = re.search(r"Full operator name: '(.+?)'", reg_out)
            if op: operator = op.group(1)

            forbidden = "Forbidden: 'yes'" in reg_out

            if reg_state == "registration-denied":
                deny_count += 1
            else:
                deny_count = 0
        else:
            consecutive_nas_fail += 1
            short_err = (reg_err or "qmi error").splitlines()[-1][:70]
            reg_state = f"NAS lỗi x{consecutive_nas_fail}: {short_err}"

            # Tự phục hồi nhẹ khi endpoint nhiễu liên tục
            if consecutive_nas_fail in (3, 6):
                warn(f"NAS fail liên tiếp ({consecutive_nas_fail}) — thử CTL sync + wait ready")
                qmicli_raw(["--ctl-sync"], timeout=6)
                wait_qmi_ready(max_wait=8)

        if deny_count >= 3 and not recovered_once:
            warn("Bị registration-denied liên tiếp — thử reset modem mode (low-power→online)")
            qmicli(["--dms-set-operating-mode=low-power"], timeout=10, retries=2)
            time.sleep(2)
            qmicli(["--dms-set-operating-mode=online"], timeout=10, retries=3)
            wait_qmi_ready(max_wait=10)
            deny_count = 0
            recovered_once = True

        if reg_out and "Registration state: 'registration-denied'" in reg_out and forbidden and not at_recovered_once:
            warn("Mạng báo forbidden — thử AT recovery (COPS auto)")
            at_recover_registration()
            at_recovered_once = True
            time.sleep(6)

        print(f"  [{elapsed:2}s] Signal: {last_sig_dbm:20} | {reg_state} {rat}", end="\r")

        if reg_state in ("registered", "roaming"):
            print(f"\n  ✓ [{elapsed:2}s] {reg_state.upper()} — {rat} — {operator or 'N/A'}")
            return True

        time.sleep(4)

    print()
    fail("Không đăng ký được mạng sau 120s")
    return False

# ─── BƯỚC 5: Kết nối data (WDS) ───────────────────────────────
def step_start_data():
    section(f"BƯỚC 5/6 — Start data connection (APN={APN})")

    if not ensure_canonical_wwan():
        fail(f"Không có netdev {IFACE} — kiểm tra udev / driver bind")
        return None

    # Enable raw IP trước khi up interface
    info("Enable raw IP mode...")
    run(["sudo", "sh", "-c", f"echo Y > /sys/class/net/{IFACE}/qmi/raw_ip"])

    # Bring up wwan0
    info(f"ip link set {IFACE} up...")
    run(["sudo", "ip", "link", "set", IFACE, "up"])
    time.sleep(1)

    # Start network — luôn gọi để có PDH+CID gắn với client hiện tại
    # (--wds-get-current-settings yêu cầu call đang được bind với cùng CID)
    pdh = cid = None
    info(f"qmicli --wds-start-network (APN={APN})...")
    for attempt in range(3):
        out, err = qmicli([
            f"--wds-start-network=apn={APN},ip-type=4",
            "--client-no-release-cid"
        ], timeout=30)

        if out:
            m_pdh = re.search(r"Packet data handle: '(\d+)'", out)
            m_cid = re.search(r"CID: '(\d+)'", out)
            if m_pdh: pdh = m_pdh.group(1)
            if m_cid: cid = m_cid.group(1)
            ok(f"Connected  PDH={pdh}  CID={cid}")
            if pdh:
                print(f"\n    [Disconnect sau này]:")
                print(f"    sudo qmicli -d {QMI_DEV} --wds-stop-network={pdh} --client-cid={cid}")
            break
        elif err and ("AlreadyConnected" in err or "interface-in-use" in err or "CallFailed" in err):
            # Modem đang có session cũ — reset WDS rồi thử lại
            if attempt < 2:
                warn(f"AlreadyConnected (lần {attempt+1}) — reset WDS rồi thử lại...")
                qmicli(["--wds-reset"], timeout=10)
                time.sleep(3)
            else:
                warn("AlreadyConnected vẫn còn sau reset — thử lấy IP từ interface")
                break
        elif err and "endpoint hangup" in err:
            if attempt < 2:
                warn(f"Endpoint hangup (lần {attempt+1}/3) — đợi 4s rồi thử lại...")
                time.sleep(4)
            else:
                fail(f"WDS start-network thất bại sau 3 lần: endpoint hangup")
                return None, None
        else:
            fail(f"WDS start-network lỗi: {err}")
            return None, None

    # Lấy IP settings qua QMI
    info("qmicli --wds-get-current-settings...")
    out2, err2 = qmicli(["--wds-get-current-settings"], timeout=10)

    # Fallback: đọc IP từ interface nếu QMI thất bại (OutOfCall / không có PDH)
    if out2 is None:
        warn(f"Không lấy được settings qua QMI ({err2}) — thử đọc IP từ {IFACE}...")
        rc, iout, _ = run(["ip", "-4", "addr", "show", "dev", IFACE])
        if rc == 0 and iout:
            m_ip = re.search(r"inet\s+(\S+)\s+peer\s+(\S+)", iout)
            if not m_ip:
                m_ip2 = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", iout)
            if m_ip:
                ip_on_iface = m_ip.group(1).split("/")[0]
                gw_on_iface = m_ip.group(2).split("/")[0]
                ok(f"IP từ interface: {ip_on_iface}  peer {gw_on_iface}")
                return ip_on_iface, gw_on_iface, None, None
            elif m_ip2:
                ip_on_iface = m_ip2.group(1)
                ok(f"IP từ interface: {ip_on_iface}/32")
                return ip_on_iface, None, None, None
        warn("Không có IP trên interface — cần disconnect rồi reconnect")
        return None, None, None, None

    ip   = re.search(r"IPv4 address:\s+(.+)",         out2)
    gw   = re.search(r"IPv4 gateway address:\s+(.+)", out2)
    dns1 = re.search(r"IPv4 primary DNS:\s+(.+)",     out2)
    dns2 = re.search(r"IPv4 secondary DNS:\s+(.+)",   out2)

    ip_addr  = ip.group(1).strip()   if ip   else None
    gw_addr  = gw.group(1).strip()   if gw   else None
    dns1_str = dns1.group(1).strip() if dns1 else None
    dns2_str = dns2.group(1).strip() if dns2 else None

    ok(f"IP:      {ip_addr}")
    ok(f"Gateway: {gw_addr or 'N/A'}")
    ok(f"DNS:     {dns1_str or 'N/A'}  {dns2_str or ''}")

    return ip_addr, gw_addr, dns1_str, dns2_str

# ─── BƯỚC 6: Cấu hình interface ───────────────────────────────
def step_configure_interface(ip_addr, gw_addr, dns1, dns2):
    section(f"BƯỚC 6/6 — Cấu hình {IFACE}")

    if not ip_addr:
        fail("Không có IP — bỏ qua cấu hình interface")
        return False

    # Đảm bảo raw_ip mode được bật (cần thiết trước khi link set up với qmi_wwan)
    run(["sudo", "sh", "-c", f"echo Y > /sys/class/net/{IFACE}/qmi/raw_ip"])

    # Xóa IP cũ — dùng flush nhưng bring up interface TRƯỚC
    run(["sudo", "ip", "link", "set", IFACE, "up"])
    time.sleep(0.5)
    run(["sudo", "ip", "addr", "flush", "dev", IFACE])

    # Set IP
    if gw_addr:
        run(["sudo", "ip", "addr", "add", ip_addr, "peer", gw_addr, "dev", IFACE])
    else:
        run(["sudo", "ip", "addr", "add", f"{ip_addr}/32", "dev", IFACE])
    ok(f"ip addr: {ip_addr}")

    # Đảm bảo interface UP với retry + kiểm tra thực sự
    iface_up = False
    for attempt in range(5):
        run(["sudo", "ip", "link", "set", IFACE, "up"])
        time.sleep(0.8)
        rc_chk, chk_out, _ = run(["ip", "link", "show", IFACE])
        has_up_flag = bool(re.search(r"<[^>]*\bUP\b[^>]*>", chk_out or ""))
        if rc_chk == 0 and has_up_flag:
            iface_up = True
            ok(f"{IFACE} interface UP (attempt {attempt+1})")
            break
        warn(f"ip link set up thử lần {attempt+1}/5 — chưa UP, thử lại...")
    if not iface_up:
        warn(f"{IFACE} vẫn chưa UP sau 5 lần thử — kiểm tra kết nối QMI")

    # DNS vào /etc/resolv.conf (chỉ nếu chưa có)
    resolv = ""
    try:
        with open("/etc/resolv.conf") as f: resolv = f.read()
    except Exception: pass
    dns_entries = ""
    for d in [dns1, dns2]:
        if d and d not in resolv:
            dns_entries += f"nameserver {d}\n"
    if dns_entries:
        run(["sudo", "sh", "-c", f'echo "{dns_entries.strip()}" >> /etc/resolv.conf'])
        ok(f"DNS thêm vào /etc/resolv.conf")

    warn("Default route KHÔNG được thêm tự động — SSH/WiFi không bị ảnh hưởng")
    print(f"    → Để dùng 4G: sudo ip route add default dev {IFACE} metric 200")

    # Hiển thị ifconfig
    rc, out, _ = run(["ip", "addr", "show", IFACE])
    print(f"\n  {IFACE} interface:")
    for line in out.splitlines():
        print(f"    {line}")

    # Ping test qua wwan0 chỉ khi interface thực sự UP
    info("Ping test 8.8.8.8 qua wwan0...")
    if not iface_up:
        warn("Bỏ qua ping test — interface chưa UP")
    else:
        # Raw IP mode: route trực tiếp qua dev, không cần via gateway
        _tmp_route = False
        rc_r, _, _ = run(["sudo", "ip", "route", "add", "8.8.8.8/32", "dev", IFACE])
        _tmp_route = (rc_r == 0)
        rc, out, _ = run(["ping", "-c", "3", "-W", "5", "-I", IFACE, "8.8.8.8"], timeout=25)
        if _tmp_route:
            run(["sudo", "ip", "route", "del", "8.8.8.8/32", "dev", IFACE])
        if rc == 0:
            ok("Internet OK qua wwan0!")
            for line in out.splitlines():
                if "packets transmitted" in line or "rtt" in line:
                    print(f"    {line}")
        else:
            warn("Ping thất bại — tín hiệu yếu hoặc cần thêm default route")
            info(f"    → sudo ip route add default dev {IFACE} metric 200")

    return True

# ─── Main ──────────────────────────────────────────────────────
def main():
    import os
    global IS_ROOT
    if os.geteuid() != 0:
        print("  ! Script cần chạy với sudo để thao tác GPIO và sysfs driver bind/unbind")
        print("  → sudo python3 Module_4G/enable_4g_auto.py")
        return 1
    IS_ROOT = True

    print("\n" + "=" * 50)
    print("  AUTO ENABLE 4G — QMI MODE (qmicli)")
    print("=" * 50)

    step_prepare_qmi_environment()

    # Bước 1: GPIO power on + boot wait
    step_gpio_power_on()

    # Bước 2: Switch sang qmi_wwan
    if not step_switch_to_qmi_driver():
        fail("Không switch được driver — dừng lại")
        _write_modem_state(False, phase="driver_bind_failed")
        return 1

    if not ensure_canonical_wwan():
        fail(f"Không có netdev {IFACE} — kiểm tra udev rule 76-dronebridge-sim7600-wwan")
        _write_modem_state(False, phase="netdev_missing")
        return 1

    # Bước 3: Verify modem
    if not step_verify_modem():
        fail("Modem/QMI hoặc SIM chưa sẵn sàng — dừng lại (xem log BƯỚC 3)")
        return 1

    # Bước 3.5: Cấu hình radio qua AT (PSM off, LTE bands, Auto mode)
    # Non-fatal: nếu AT port không có sẵn, vẫn tiếp tục với QMI
    step_configure_modem_at()

    # Bước 4: Chờ đăng ký mạng
    if not step_wait_network():
        fail("Không có mạng — dừng lại")
        return 1

    # Bước 5: Start data + lấy IP
    result = step_start_data()
    if result is None:
        fail("Không start data được")
        return 1

    # step_start_data trả về (ip, gw, dns1, dns2) hoặc (pdh, cid) nếu có lỗi
    if len(result) == 4:
        ip_addr, gw_addr, dns1, dns2 = result
    else:
        ip_addr, gw_addr, dns1, dns2 = None, None, None, None

    # Bước 6: Cấu hình interface
    step_configure_interface(ip_addr, gw_addr, dns1, dns2)

    final_ip = _get_wwan_ipv4()
    if not final_ip:
        fail(f"{IFACE} không có IPv4 sau cấu hình — init thất bại")
        _write_modem_state(False, phase="no_ipv4")
        return 1

    _write_modem_state(True, ip=final_ip, phase="active")
    print("\n" + "=" * 50)
    print("  HOÀN TẤT — 4G QMI MODE ACTIVE")
    print("=" * 50)
    return 0

if __name__ == "__main__":
    try:
        exit(main())
    except KeyboardInterrupt:
        print("\n  ! Người dùng đã dừng script (Ctrl+C)")
        _gpio_release()
        exit(130)
