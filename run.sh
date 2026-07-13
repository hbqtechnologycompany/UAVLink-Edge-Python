#!/usr/bin/env bash
# Chạy UAVLink-Edge bằng đúng venv (tránh sudo / alias python hệ thống).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/venv/bin/python" "$ROOT/main.py" "$@"
