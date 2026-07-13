"""Deprecated — partner HEARTBEAT is integrated in forwarder.py (shared UDP socket)."""

from __future__ import annotations

import logging

logger = logging.getLogger("PartnerHB")


def start_partner_heartbeat(cfg) -> None:
    logger.debug("[PARTNER_HB] handled by forwarder (shared pixhawk UDP socket)")
