#!/usr/bin/env python3
"""
Nhà mạng Việt Nam — IMSI prefix, APN data mặc định, resolve APN tự động.
"""

from __future__ import annotations

import os
import re

# MCC 452 — MNC 2–3 chữ số (thử dài trước khi khớp prefix)
VN_CARRIERS: dict[str, str] = {
    "45204": "Viettel",
    "45201": "MobiFone",
    "45202": "Vinaphone",
    "45205": "Vietnamobile",
    "45207": "Gmobile",
}

# APN data LTE phổ biến (có thể override bằng DRONEBRIDGE_APN)
VN_APN: dict[str, str] = {
    "Viettel": "v-internet",
    "MobiFone": "m-wap",
    "Vinaphone": "m3-world",
    "Vietnamobile": "internet",
    "Gmobile": "internet",
}

# Xác thực APN theo hướng dẫn nhà mạng (Local SIM VN)
VN_APN_PROFILE: dict[str, dict[str, str]] = {
    "MobiFone": {
        "username": "mms",
        "password": "mms",
        "auth": "pap",
    },
}

# Alias tên nhà mạng từ QMI / AT+COPS
_CARRIER_ALIASES: dict[str, str] = {
    "viettel": "Viettel",
    "mobifone": "MobiFone",
    "mobi fone": "MobiFone",
    "vinaphone": "Vinaphone",
    "vina phone": "Vinaphone",
    "vietnamobile": "Vietnamobile",
    "gmobile": "Gmobile",
    "beeline": "Gmobile",
}


def carrier_from_imsi(imsi: str | None) -> str | None:
    if not imsi:
        return None
    digits = "".join(c for c in imsi if c.isdigit())
    if len(digits) < 5:
        return None
    for plen in (5, 4, 3):
        prefix = digits[:plen]
        if prefix in VN_CARRIERS:
            return VN_CARRIERS[prefix]
    return None


def normalize_carrier_name(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().lower()
    if key in _CARRIER_ALIASES:
        return _CARRIER_ALIASES[key]
    for alias, canonical in _CARRIER_ALIASES.items():
        if alias in key:
            return canonical
    return name.strip()


def apn_for_carrier(carrier: str | None) -> str | None:
    if not carrier:
        return None
    c = normalize_carrier_name(carrier) or carrier
    return VN_APN.get(c)


def resolve_apn(
    imsi: str | None = None,
    carrier: str | None = None,
    env_override: str | None = None,
) -> tuple[str, str, str]:
    """
  Trả về (apn, carrier_name, source).
  source: env | imsi | carrier | default
    """
    override = (env_override or os.getenv("DRONEBRIDGE_APN", "")).strip()
    if override:
        c = normalize_carrier_name(carrier) or carrier_from_imsi(imsi) or "Unknown"
        return override, c, "env"

    c = normalize_carrier_name(carrier) or carrier_from_imsi(imsi)
    if c:
        apn = VN_APN.get(c)
        if apn:
            return apn, c, "imsi" if carrier_from_imsi(imsi) == c else "carrier"

    return VN_APN["Viettel"], "Viettel", "default"


def resolve_wds_profile(
    imsi: str | None = None,
    carrier: str | None = None,
    env_override: str | None = None,
) -> tuple[dict[str, str], str, str]:
    """
    Tham số qmicli --wds-start-network (APN + auth nếu có).
    MobiFone Local SIM: m-wap + user/pass mms, PAP (theo hướng dẫn nhà mạng).
    """
    apn, carrier_name, source = resolve_apn(imsi=imsi, carrier=carrier, env_override=env_override)
    profile: dict[str, str] = {"apn": apn, "ip-type": "4"}

    base = VN_APN_PROFILE.get(carrier_name, {})
    user = os.getenv("DRONEBRIDGE_APN_USER", "").strip() or base.get("username", "")
    password = os.getenv("DRONEBRIDGE_APN_PASSWORD", "").strip() or base.get("password", "")
    auth = os.getenv("DRONEBRIDGE_APN_AUTH", "").strip().lower() or base.get("auth", "")

    if user:
        profile["username"] = user
    if password:
        profile["password"] = password
    if auth:
        profile["auth"] = auth

    return profile, carrier_name, source


def format_wds_start_network(profile: dict[str, str]) -> str:
    order = ("apn", "username", "password", "auth", "ip-type")
    parts = []
    for key in order:
        if key in profile and profile[key]:
            parts.append(f"{key}={profile[key]}")
    for key, val in profile.items():
        if key not in order and val:
            parts.append(f"{key}={val}")
    return f"--wds-start-network={','.join(parts)}"


def parse_imsi_from_at(resp: str) -> str | None:
    for line in (resp or "").splitlines():
        digits = "".join(c for c in line if c.isdigit())
        if len(digits) >= 10 and digits.startswith("452"):
            return digits
    return None


def parse_ccid_from_at(resp: str) -> str | None:
    m = re.search(r"\+CCID:\s*(\d+)", resp or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r"(\d{18,22})", resp or "")
    return m.group(1) if m else None


def parse_phone_from_cnum(resp: str) -> str | None:
    if "+CNUM:" not in (resp or ""):
        return None
    # +CNUM: "","+84901234567",129,7,4
    m = re.search(r'\+CNUM:\s*"[^"]*"\s*,\s*"([^"]+)"', resp)
    if m:
        num = m.group(1).strip()
        if num and (num.startswith("+") or num.isdigit()):
            return num
    for part in resp.split('"'):
        p = part.strip()
        if p.startswith("+84") or (p.startswith("+") and len(p) >= 10):
            return p
        if p.isdigit() and len(p) >= 9:
            return p
    return None
