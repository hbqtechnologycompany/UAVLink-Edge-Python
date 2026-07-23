#!/usr/bin/env python3
"""
Cài đặt đầy đủ UAVLink-Edge-Python: apt + venv + pip.

Chạy một lần:
    python3 install.py

Tương đương:
    pip install -r requirements.txt   (trong venv — tự cài apt qua ./_bootstrap)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / "venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
REQUIREMENTS = ROOT / "requirements.txt"


def _run(cmd: list[str], **kwargs) -> int:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, **kwargs).returncode


def ensure_venv() -> Path:
    if not VENV_PYTHON.is_file():
        print("[install] Tạo venv…")
        if _run([sys.executable, "-m", "venv", str(VENV_DIR)]) != 0:
            sys.exit(1)
    return VENV_PYTHON


def install_pip(python: Path, pip_only: bool = False) -> None:
    if not pip_only:
        install_apt_only()
    if _run([str(python), "-m", "pip", "install", "--upgrade", "pip"]) != 0:
        sys.exit(1)
    env = os.environ.copy()
    env["UAVLINK_SKIP_APT"] = "1"
    if _run([str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)], env=env) != 0:
        sys.exit(1)


def install_apt_only() -> None:
    sys.path.insert(0, str(ROOT / "_bootstrap"))
    from _apt import apt_install  # noqa: E402

    if not apt_install():
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cài apt + venv + pip cho UAVLink-Edge-Python")
    parser.add_argument("--apt-only", action="store_true", help="Chỉ cài gói apt")
    parser.add_argument("--pip-only", action="store_true", help="Chỉ pip (bỏ qua apt)")
    parser.add_argument("--no-venv", action="store_true", help="Dùng python hiện tại, không tạo venv")
    args = parser.parse_args()

    if args.apt_only:
        install_apt_only()
        print("\n[install] Xong (apt). Tiếp: source venv/bin/activate && pip install -r requirements.txt")
        return

    python = Path(sys.executable) if args.no_venv else ensure_venv()
    install_pip(python, pip_only=args.pip_only)

    print("\n[install] Xong.")
    if not args.no_venv:
        print("  source venv/bin/activate")
    print("  python main.py --register    # lần đầu")
    print("  sudo python main.py          # chạy (VPN cần root)")


if __name__ == "__main__":
    main()
