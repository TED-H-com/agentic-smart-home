"""Bounded native setup forms qualified against Core 2026.10.1/.2.

The reported Shelly model is a bounded qualification, not proof of generation
or physical identity. Core's model-name helper can fall back to device input.
References: components/{ipp,shelly}/config_flow.py; shelly/{utils,coordinator}.py.
No generic flow getter, arbitrary endpoints, or model writes.
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
    sources: frozenset[str] = frozenset({"zeroconf"})

    def identity(self, value):
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError("device_setup_identity_unavailable")
        if self.domain in {"shelly", "synology_dsm"}:
            if not re.fullmatch(r"[0-9a-fA-F]{12}", value):
                raise ValueError("device_setup_identity_unavailable")
            value = value.upper()
        return [self.domain, value]

    def validate_model(self, form):
        if self.reported_model is not None:
            placeholders = form.get("description_placeholders")
            if not isinstance(placeholders, dict) or placeholders.get("model") != self.reported_model:
                raise ValueError("device_setup_model_unsupported")

    def validate_form(self, form):
        if form.get("data_schema") != []:
            raise ValueError("device_setup_flow_unsupported")
        self.validate_model(form)


LINK_SCHEMA = [
    {"type": "string", "name": "username", "required": True, "default": ""},
    {"type": "string", "name": "password", "required": True, "default": ""},
    {"type": "string", "name": "port", "required": False, "optional": True, "default": ""},
    {"type": "boolean", "name": "ssl", "required": False, "optional": True, "default": True},
    {"type": "boolean", "name": "verify_ssl", "required": False, "optional": True, "default": False},
]
LOGIN_ERRORS = {"invalid_auth": "username", "cannot_connect": "host", "unknown": "base", "missing_data": "base"}


@dataclass(frozen=True)
class SynologySetup(EmptyConfirmation):
    def validate_form(self, form):
        placeholders = form.get("description_placeholders")
        schema = form.get("data_schema")
        if (schema != LINK_SCHEMA or any(type(field[key]) is not type(expected[key])
                for field, expected in zip(schema, LINK_SCHEMA, strict=True) for key in expected)
                or not isinstance(placeholders, dict)
                or set(placeholders) != {"name", "host"}
                or not all(isinstance(value, str) for value in placeholders.values())):
            raise ValueError("device_setup_flow_unsupported")

    def response(self, raw, flow_id):
        """Drop secret-bearing defaults and result data before leaving this parser."""
        if (not isinstance(raw, dict) or raw.get("flow_id") != flow_id or raw.get("handler") != self.domain):
            raise ValueError("unconfirmed_response")
        result = {"type": raw.get("type"), "flow_id": flow_id, "handler": self.domain}
        if result["type"] == "create_entry":
            entry = raw.get("result")
            if (not isinstance(entry, dict) or not all(isinstance(entry.get(key), str)
                    for key in ("entry_id", "domain", "source"))):
                raise ValueError("unconfirmed_entry")
            return {**result, "result": {key: entry.get(key) for key in ("entry_id", "domain", "source")}}
        if result["type"] == "abort":
            return {**result, "reason": "already_configured" if raw.get("reason") in {
                "already_configured", "reconfigure_successful"} else "other"}
        step, schema, errors = raw.get("step_id"), raw.get("data_schema"), raw.get("errors")
        if result["type"] != "form" or not isinstance(schema, list) or not isinstance(errors, dict):
            raise ValueError("unconfirmed_form")
        error, options, path = None, [], None
        if step == "link":
            # Defaults can contain the submitted password. Never copy or hash them.
            shape = [{key: value for key, value in field.items() if key != "default"}
                     for field in schema if isinstance(field, dict)]
            expected = [{key: value for key, value in field.items() if key != "default"} for field in LINK_SCHEMA]
            if (shape != expected or len(schema) != len(expected)
                    or any(type(field[key]) is not type(template[key])
                           for field, template in zip(shape, expected, strict=True) for key in template)):
                raise ValueError("unconfirmed_schema")
            shape = [{key: field[key] for key in ("name", "type", "required")} for field in expected]
            if errors:
                error = next((code for code, field in LOGIN_ERRORS.items() if errors == {field: code}), None)
                if error is None:
                    raise ValueError("unconfirmed_error")
        elif step == "2sa":
            if schema != [{"type": "string", "name": "otp_code", "required": True}]:
                raise ValueError("unconfirmed_schema")
            if errors:
                if errors != {"otp_code": "otp_failed"}:
                    raise ValueError("unconfirmed_error")
                error = "otp_failed"
            shape = schema
        elif step == "backup_share":
            if errors or len(schema) != 2 or not all(isinstance(field, dict) for field in schema):
                raise ValueError("unconfirmed_schema")
            share, backup = schema
            selector = share.get("selector")
            if (share.get("name") != "backup_share" or share.get("required") is not True
                    or share.get("type") not in {None, "selector"}
                    or not isinstance(selector, dict) or set(selector) != {"select"}
                    or not isinstance(selector["select"], dict)):
                raise ValueError("unconfirmed_schema")
            select = selector["select"]
            if (select.get("multiple", False) is not False or select.get("custom_value", False) is not False
                    or select.get("mode", "dropdown") != "dropdown"):
                raise ValueError("unconfirmed_schema")
            rows = select.get("options")
            if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
                raise ValueError("unconfirmed_options")
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {"value", "label"}
                        or not isinstance(row["value"], str) or not re.fullmatch(r"/[^\x00-\x1f]{1,255}", row["value"])
                        or not isinstance(row["label"], str) or len(row["label"]) > 160):
                    raise ValueError("unconfirmed_options")
                options.append({"value": row["value"], "label": row["label"]})
            if len({row["value"] for row in options}) != len(options):
                raise ValueError("unconfirmed_options")
            path = backup.get("default")
            if (backup.get("name") != "backup_path" or backup.get("type") != "string"
                    or backup.get("required") is not True or not isinstance(path, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", path)):
                raise ValueError("unconfirmed_schema")
            shape = [{"name": "backup_share", "type": "select", "required": True},
                     {"name": "backup_path", "type": "string", "required": True}]
        else:
            raise ValueError("unconfirmed_step")
        return {**result, "step_id": step, "error": error, "schema": shape, "options": options, "backup_path": path}


ADAPTERS = {
    "ipp": EmptyConfirmation("ipp", "zeroconf_confirm", 2, "IPP 印表機", "IPP",
        "確認後會先備份 Home Assistant 設定，再接入這台 IPP 印表機；不會列印或授予 AI 控制權限。"),
    "shelly": EmptyConfirmation("shelly", "confirm_discovery", 2, "Shelly 1 設備", "Shelly 1",
        "確認後會先備份 Home Assistant 設定，再接入 HA 回報為 Shelly 1 的設備。原生整合可能設定設備連線；備份只涵蓋 HA 設定，不切換繼電器，也不授予 AI 控制權限。",
        reported_model="Shelly 1"),
    "synology_dsm": SynologySetup("synology_dsm", "link", 3, "Synology NAS", "Synology NAS",
        "確認後會先備份家庭設定，再用你輸入的帳號登入這台 NAS 並接入。帳號密碼只交給家庭系統保存，不會傳給管家，也不授予 AI 控制權限。",
        sources=frozenset({"ssdp", "zeroconf"})),
}
