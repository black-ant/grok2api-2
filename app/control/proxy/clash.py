"""Clash YAML parsing for the native HTTP/SOCKS egress stack.

The application can connect directly through HTTP(S) and SOCKS proxies. It
does not embed a Mihomo or Xray runtime, so other Clash node protocols are
returned as visible-but-unavailable candidates instead of being converted to
an invalid URL.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import yaml


MAX_CLASH_YAML_BYTES = 8 * 1024 * 1024
_SUPPORTED_TYPES = {
    "http": "http",
    "https": "https",
    "socks": "socks5",
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks4": "socks4",
    "socks4a": "socks4",
}


class ClashParseError(ValueError):
    """Raised when a Clash YAML payload cannot produce proxy candidates."""


@dataclass(frozen=True)
class ClashProxyCandidate:
    """A parsed proxy node and its optional native egress URL."""

    proxy_id: str
    name: str
    proxy_type: str
    server: str
    port: int
    supported: bool
    reason: str = ""
    proxy_url: str = ""

    def public_dict(self) -> dict[str, Any]:
        """Return the safe representation used by admin APIs."""
        return {
            "id": self.proxy_id,
            "name": self.name,
            "type": self.proxy_type,
            "server": self.server,
            "port": self.port,
            "supported": self.supported,
            "reason": self.reason,
        }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_port(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return port if 1 <= port <= 65535 else 0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _as_text(value).lower() in {"1", "true", "yes", "on"}


def _host_for_url(server: str) -> str:
    if server.startswith("[") and server.endswith("]"):
        return server
    if ":" in server:
        return f"[{server}]"
    return server


def _build_native_url(
    node: dict[str, Any], proxy_type: str, server: str, port: int
) -> str:
    scheme = _SUPPORTED_TYPES[proxy_type]
    if proxy_type == "http" and _as_bool(node.get("tls")):
        scheme = "https"

    username = _as_text(node.get("username"))
    password = _as_text(node.get("password"))
    credentials = ""
    if username or password:
        username_encoded = quote(username, safe="")
        password_encoded = quote(password, safe="")
        credentials = f"{username_encoded}:{password_encoded}@"
    return f"{scheme}://{credentials}{_host_for_url(server)}:{port}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_safe(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _proxy_id(node: dict[str, Any]) -> str:
    payload = json.dumps(
        _json_safe(node),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"clash-{digest[:20]}"


def _extract_nodes(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_nodes = payload
    elif isinstance(payload, dict):
        if "proxies" in payload:
            raw_nodes = payload["proxies"]
        elif "type" in payload:
            raw_nodes = [payload]
        else:
            raise ClashParseError("YAML 中未找到 proxies 节点")
    else:
        raise ClashParseError("YAML 根节点必须是对象或数组")

    if not isinstance(raw_nodes, list):
        raise ClashParseError("proxies 必须是数组")

    nodes = [item for item in raw_nodes if isinstance(item, dict)]
    if not nodes:
        raise ClashParseError("YAML 中没有可用的代理节点")
    return nodes


def parse_clash_yaml(raw: str) -> list[ClashProxyCandidate]:
    """Parse Clash YAML into candidates without starting a proxy core."""
    if not isinstance(raw, str) or not raw.strip():
        raise ClashParseError("请输入 Clash YAML 内容")
    if len(raw.encode("utf-8")) > MAX_CLASH_YAML_BYTES:
        raise ClashParseError("Clash YAML 不能超过 8 MB")

    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        message = str(exc).splitlines()[0].strip() or "YAML 格式无效"
        raise ClashParseError(f"YAML 解析失败：{message}") from exc

    nodes = _extract_nodes(payload)
    candidates: list[ClashProxyCandidate] = []
    seen_ids: dict[str, int] = {}

    for index, node in enumerate(nodes, start=1):
        name = _as_text(node.get("name")) or f"节点 {index}"
        proxy_type = _as_text(node.get("type")).lower()
        server = _as_text(node.get("server"))
        port = _as_port(node.get("port"))
        proxy_id = _proxy_id(node)
        occurrence = seen_ids.get(proxy_id, 0)
        seen_ids[proxy_id] = occurrence + 1
        if occurrence:
            proxy_id = f"{proxy_id}-{occurrence + 1}"

        if proxy_type not in _SUPPORTED_TYPES:
            display_type = proxy_type or "未知"
            reason = f"当前请求栈不支持 {display_type}，需要 Mihomo/Xray 内核"
            candidates.append(
                ClashProxyCandidate(
                    proxy_id=proxy_id,
                    name=name,
                    proxy_type=proxy_type or "未知",
                    server=server or "-",
                    port=port,
                    supported=False,
                    reason=reason,
                )
            )
            continue

        if not server or not port:
            candidates.append(
                ClashProxyCandidate(
                    proxy_id=proxy_id,
                    name=name,
                    proxy_type=proxy_type,
                    server=server or "-",
                    port=port,
                    supported=False,
                    reason="缺少有效的 server 或 port",
                )
            )
            continue

        candidates.append(
            ClashProxyCandidate(
                proxy_id=proxy_id,
                name=name,
                proxy_type=proxy_type,
                server=server,
                port=port,
                supported=True,
                proxy_url=_build_native_url(node, proxy_type, server, port),
            )
        )

    if not candidates:
        raise ClashParseError("YAML 中没有代理节点")
    return candidates


def find_clash_candidate(raw: str, proxy_id: str) -> ClashProxyCandidate:
    """Resolve one supported candidate from the exact YAML payload."""
    target = _as_text(proxy_id)
    if not target:
        raise ClashParseError("请选择一个代理节点")
    for candidate in parse_clash_yaml(raw):
        if candidate.proxy_id == target:
            if not candidate.supported:
                raise ClashParseError(candidate.reason or "所选节点不可用")
            return candidate
    raise ClashParseError("所选代理节点不在当前 YAML 中")


__all__ = [
    "ClashParseError",
    "ClashProxyCandidate",
    "MAX_CLASH_YAML_BYTES",
    "find_clash_candidate",
    "parse_clash_yaml",
]
