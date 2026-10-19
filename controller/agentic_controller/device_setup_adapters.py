"""Exact native empty confirmations qualified against Core 2026.10.1/.2.

The reported Shelly model is a bounded qualification, not proof of generation
or physical identity. Core's model-name helper can fall back to device input.
References: components/{ipp,shelly}/config_flow.py; shelly/{utils,coordinator}.py.
No generic flow getter, credentials, arbitrary endpoints, or model writes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EmptyConfirmation:
    domain: str
    step: str
    record_version: int
    label: str
    backup_label: str
    proposal_message: str
    reported_model: str | None = None

    def identity(self, value):
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError("device_setup_identity_unavailable")
        if self.domain == "shelly":
            if not re.fullmatch(r"[0-9a-fA-F]{12}", value):
                raise ValueError("device_setup_identity_unavailable")
            value = value.upper()
        return [self.domain, value]

    def validate_model(self, form):
        if self.reported_model is not None:
            placeholders = form.get("description_placeholders")
            if not isinstance(placeholders, dict) or placeholders.get("model") != self.reported_model:
                raise ValueError("device_setup_model_unsupported")


ADAPTERS = {
    "ipp": EmptyConfirmation("ipp", "zeroconf_confirm", 2, "IPP 印表機", "IPP",
        "確認後會先備份 Home Assistant 設定，再接入這台 IPP 印表機；不會列印或授予 AI 控制權限。"),
    "shelly": EmptyConfirmation("shelly", "confirm_discovery", 2, "Shelly 1 設備", "Shelly 1",
        "確認後會先備份 Home Assistant 設定，再接入 HA 回報為 Shelly 1 的設備。原生整合可能設定設備連線；備份只涵蓋 HA 設定，不切換繼電器，也不授予 AI 控制權限。",
        reported_model="Shelly 1"),
}
