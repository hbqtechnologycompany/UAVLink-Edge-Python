#!/usr/bin/env bash
# Chẩn đoán nhanh 4G — không cần sudo cho phần AT/USB; QMI cần root.
set -euo pipefail

echo "========== 4G DIAGNOSE $(date -Iseconds) =========="

echo ""
echo "--- USB modem ---"
lsusb 2>/dev/null | grep -iE '1e0e|simcom|qualcomm' || echo "(không thấy SIM7600 trên USB)"

echo ""
echo "--- Devices ---"
ls -la /dev/cdc-wdm0 /dev/ttyUSB* 2>/dev/null || echo "(chưa có cdc-wdm0 / ttyUSB)"

echo ""
echo "--- wwan0 ---"
ip -br addr show wwan0 2>/dev/null || echo "wwan0: chưa có"

echo ""
echo "--- AT (không cần sudo nếu user trong dialout) ---"
python3 - <<'PY'
import time
try:
    import serial
except ImportError:
    print("  ! thiếu pyserial")
    raise SystemExit(0)

for p in ["/dev/ttyUSB2", "/dev/ttyUSB1", "/dev/ttyUSB0"]:
    try:
        s = serial.Serial(p, 115200, timeout=2)
        s.write(b"AT\r\n")
        time.sleep(0.4)
        at = s.read(200).decode("utf-8", "replace")
        s.write(b"AT+CPIN?\r\n")
        time.sleep(1)
        cpin = s.read(300).decode("utf-8", "replace")
        s.close()
        if "OK" in at:
            print(f"  {p}: AT OK")
            for line in cpin.splitlines():
                if "CPIN" in line or "CME ERROR" in line:
                    print(f"    {line.strip()}")
            break
    except Exception as e:
        print(f"  {p}: {e}")
PY

echo ""
echo "--- QMI (cần sudo) ---"
if [ "$(id -u)" -eq 0 ]; then
    qmicli -d /dev/cdc-wdm0 --uim-get-card-status 2>&1 | grep -E "Card state|PIN" || true
    qmicli -d /dev/cdc-wdm0 --nas-get-serving-system 2>&1 | grep -E "Registration|Roaming|CS|PS" || true
else
    echo "  → sudo bash $0   (hoặc sudo qmicli -d /dev/cdc-wdm0 --uim-get-card-status)"
fi

echo ""
echo "--- systemd 4g-init (5 dòng cuối) ---"
journalctl -u dronebridge-4g-init.service -n 5 --no-pager 2>/dev/null || true

echo ""
echo "========== Kết luận nhanh =========="
echo "  • Modem USB + GPIO + QMI OK  →  lỗi thường là SIM chưa gắn / không tiếp xúc (no-ATR)"
echo "  • SIM present nhưng không sóng  →  chạy: sudo python3 .../enable_4g_auto.py"
echo "  • Full init: sudo systemctl start dronebridge-4g-init.service"
