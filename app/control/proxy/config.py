"""Proxy clearance config helpers shared by control and dataplane code."""

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.platform.config.snapshot import get_config


@dataclass(frozen=True)
class ClearanceConfig:
    cf_cookies: str = ""
    user_agent: str = ""
    cf_clearance: str = ""
    browser: str = ""


def _cfg_str(cfg: Any, key: str) -> str:
    value = cfg.get_str(key, "")
    return value if value.strip() else ""


def first_config_str(cfg: Any, *keys: str) -> str:
    for key in keys:
        value = _cfg_str(cfg, key)
        if value:
            return value
    return ""


def resolve_clearance_config(cfg: Any | None = None) -> ClearanceConfig:
    cfg = cfg or get_config()
    return ClearanceConfig(
        cf_cookies=first_config_str(
            cfg,
            "proxy.cf_cookies",
            "proxy.clearance.cf_cookies",
        ),
        user_agent=first_config_str(
            cfg,
            "proxy.user_agent",
            "proxy.clearance.user_agent",
        ),
        cf_clearance=first_config_str(
            cfg,
            "proxy.cf_clearance",
            "proxy.clearance.cf_clearance",
        ),
        browser=first_config_str(
            cfg,
            "proxy.browser",
            "proxy.clearance.browser",
        ),
    )


def normalize_legacy_http_proxy(value: str) -> str:
    """Rewrite the legacy ``host:port:user:password`` proxy form into a URL.

    Providers commonly hand out that plain form. Anything that does not match
    exactly (IPv4 host, numeric port, non-empty credentials) is returned
    unchanged so every other format keeps working as before.
    """
    text = str(value or "")
    parts = text.split(":", 3)
    if len(parts) != 4:
        return text
    host, port_text, username, password = parts
    if not username or not password:
        return text
    try:
        if ipaddress.ip_address(host).version != 4:
            return text
    except ValueError:
        return text
    try:
        port = int(port_text, 10)
    except ValueError:
        return text
    if not 0 < port <= 65535:
        return text
    return (
        f"http://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}"
    )


__all__ = [
    "ClearanceConfig",
    "first_config_str",
    "normalize_legacy_http_proxy",
    "resolve_clearance_config",
]
