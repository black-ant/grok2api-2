"""Local proxy-core bridges for Clash nodes."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.paths import data_path

from .clash import ClashProxyCandidate
from .kernels import (
    KERNEL_MIHOMO,
    KERNEL_NATIVE,
    KERNEL_SING_BOX,
    KERNEL_XRAY,
    ProxyKernelManager,
    normalize_kernel,
)


class KernelBridgeError(RuntimeError):
    """Raised when a local proxy-core bridge cannot be started."""


def _text(node: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = node.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _int(node: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = node.get(key)
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 1 <= parsed <= 65535:
            return parsed
    return 0


def _bool(node: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = node.get(key)
        if isinstance(value, bool):
            return value
        if str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _node_name(node: dict[str, Any]) -> str:
    return _text(node, "name") or "proxy-node"


def _server(node: dict[str, Any]) -> tuple[str, int]:
    server = _text(node, "server")
    port = _int(node, "port")
    if not server or not port:
        raise KernelBridgeError("节点缺少有效的 server 或 port")
    return server, port


def _sni(node: dict[str, Any]) -> str:
    return _text(node, "sni", "servername", "server-name")


def _tls_enabled(node: dict[str, Any]) -> bool:
    return _bool(node, "tls") or bool(_mapping(node.get("reality-opts")))


def _transport_options(node: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    network = _text(node, "network").lower() or "tcp"
    if network == "ws":
        ws_options = _mapping(node.get("ws-opts"))
        headers = _mapping(ws_options.get("headers"))
        if not headers:
            host = _text(node, "ws-host", "host")
            headers = {"Host": host} if host else {}
        return "ws", {
            "path": str(ws_options.get("path") or _text(node, "ws-path") or "/"),
            "headers": headers,
        }
    if network == "grpc":
        grpc_options = _mapping(node.get("grpc-opts"))
        service_name = _text(
            grpc_options,
            "grpc-service-name",
            "service-name",
            "serviceName",
        ) or _text(node, "grpc-service-name", "service-name", "serviceName")
        return "grpc", {"serviceName": service_name} if service_name else {}
    if network in {"http", "h2"}:
        http_options = _mapping(node.get("h2-opts"))
        hosts = _strings(http_options.get("host")) or _strings(node.get("host"))
        return "http", {
            "path": str(http_options.get("path") or _text(node, "path") or "/"),
            "host": hosts,
        }
    return network, {}


def _xray_stream(node: dict[str, Any]) -> dict[str, Any]:
    network, transport = _transport_options(node)
    stream: dict[str, Any] = {"network": network}
    if network == "ws":
        stream["wsSettings"] = {
            "path": transport.get("path", "/"),
            "headers": transport.get("headers", {}),
        }
    elif network == "grpc" and transport:
        stream["grpcSettings"] = transport
    elif network == "http":
        stream["httpSettings"] = transport

    reality = _mapping(node.get("reality-opts"))
    sni = _sni(node)
    fingerprint = _text(node, "client-fingerprint") or "chrome"
    if reality:
        reality_settings: dict[str, Any] = {
            "show": False,
            "fingerprint": fingerprint,
            "spiderX": _text(node, "spider-x", "spiderX") or "/",
        }
        if sni:
            reality_settings["serverName"] = sni
        public_key = _text(reality, "public-key", "public_key")
        short_id = _text(reality, "short-id", "short_id")
        if public_key:
            reality_settings["publicKey"] = public_key
        if short_id:
            reality_settings["shortId"] = short_id
        stream["security"] = "reality"
        stream["realitySettings"] = reality_settings
    elif _tls_enabled(node):
        tls_settings: dict[str, Any] = {
            "allowInsecure": _bool(node, "skip-cert-verify")
        }
        if sni:
            tls_settings["serverName"] = sni
        alpn = _strings(node.get("alpn"))
        if alpn:
            tls_settings["alpn"] = alpn
        if fingerprint:
            tls_settings["fingerprint"] = fingerprint
        stream["security"] = "tls"
        stream["tlsSettings"] = tls_settings
    return stream


def _xray_outbound(node: dict[str, Any]) -> dict[str, Any]:
    proxy_type = _text(node, "type").lower()
    server, port = _server(node)
    if proxy_type == "vless":
        user: dict[str, Any] = {
            "id": _text(node, "uuid"),
            "encryption": "none",
        }
        flow = _text(node, "flow")
        if flow:
            user["flow"] = flow
        outbound = {
            "protocol": "vless",
            "tag": "proxy-out",
            "settings": {"vnext": [{"address": server, "port": port, "users": [user]}]},
        }
    elif proxy_type == "vmess":
        user = {
            "id": _text(node, "uuid"),
            "security": _text(node, "cipher") or "auto",
            "alterId": int(node.get("alterId") or node.get("alter-id") or 0),
        }
        outbound = {
            "protocol": "vmess",
            "tag": "proxy-out",
            "settings": {"vnext": [{"address": server, "port": port, "users": [user]}]},
        }
    elif proxy_type == "trojan":
        server_config: dict[str, Any] = {
            "address": server,
            "port": port,
            "password": _text(node, "password"),
        }
        flow = _text(node, "flow")
        if flow:
            server_config["flow"] = flow
        outbound = {
            "protocol": "trojan",
            "tag": "proxy-out",
            "settings": {"servers": [server_config]},
        }
    elif proxy_type in {"ss", "shadowsocks"}:
        server_config = {
            "address": server,
            "port": port,
            "method": _text(node, "cipher", "method"),
            "password": _text(node, "password"),
        }
        plugin = _text(node, "plugin")
        if plugin:
            server_config["plugin"] = plugin
            plugin_opts = _mapping(node.get("plugin-opts"))
            if plugin_opts:
                server_config["pluginOpts"] = plugin_opts
        outbound = {
            "protocol": "shadowsocks",
            "tag": "proxy-out",
            "settings": {"servers": [server_config]},
        }
    else:
        raise KernelBridgeError(f"Xray 不支持 Clash 节点类型：{proxy_type or '未知'}")

    outbound["streamSettings"] = _xray_stream(node)
    return outbound


def build_xray_config(node: dict[str, Any], port: int) -> dict[str, Any]:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "socks",
                "settings": {"udp": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            }
        ],
        "outbounds": [
            _xray_outbound(node),
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [{"type": "field", "inboundTag": ["socks-in"], "outboundTag": "proxy-out"}],
        },
    }


def _sing_box_tls(node: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    reality = _mapping(node.get("reality-opts"))
    tls: dict[str, Any] = {
        "enabled": force or _tls_enabled(node),
        "insecure": _bool(node, "skip-cert-verify"),
    }
    sni = _sni(node)
    if sni:
        tls["server_name"] = sni
    fingerprint = _text(node, "client-fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    if reality:
        reality_config: dict[str, Any] = {"enabled": True}
        public_key = _text(reality, "public-key", "public_key")
        short_id = _text(reality, "short-id", "short_id")
        if public_key:
            reality_config["public_key"] = public_key
        if short_id:
            reality_config["short_id"] = short_id
        tls["reality"] = reality_config
    alpn = _strings(node.get("alpn"))
    if alpn:
        tls["alpn"] = alpn
    return tls


def _sing_box_transport(node: dict[str, Any]) -> dict[str, Any] | None:
    network, transport = _transport_options(node)
    if network == "ws":
        result: dict[str, Any] = {"type": "ws", "path": transport.get("path", "/")}
        if transport.get("headers"):
            result["headers"] = transport["headers"]
        return result
    if network == "grpc":
        return {"type": "grpc", "service_name": transport.get("serviceName", "")}
    return None


def _sing_box_outbound(node: dict[str, Any]) -> dict[str, Any]:
    proxy_type = _text(node, "type").lower()
    server, port = _server(node)
    if proxy_type == "vless":
        outbound: dict[str, Any] = {
            "type": "vless",
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
            "uuid": _text(node, "uuid"),
        }
        flow = _text(node, "flow")
        if flow:
            outbound["flow"] = flow
    elif proxy_type == "vmess":
        outbound = {
            "type": "vmess",
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
            "uuid": _text(node, "uuid"),
            "security": _text(node, "cipher") or "auto",
            "alter_id": int(node.get("alterId") or node.get("alter-id") or 0),
        }
    elif proxy_type == "trojan":
        outbound = {
            "type": "trojan",
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
            "password": _text(node, "password"),
        }
    elif proxy_type in {"ss", "shadowsocks"}:
        if _text(node, "plugin"):
            raise KernelBridgeError("sing-box 当前不接受带插件的 Shadowsocks Clash 节点")
        outbound = {
            "type": "shadowsocks",
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
            "method": _text(node, "cipher", "method"),
            "password": _text(node, "password"),
        }
    elif proxy_type in {"hysteria", "hysteria2", "tuic", "anytls"}:
        outbound = {
            "type": proxy_type,
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
        }
        if proxy_type == "hysteria":
            outbound["auth_str"] = _text(node, "auth-str", "auth_str", "auth", "password")
        elif proxy_type == "hysteria2":
            outbound["password"] = _text(node, "password")
        elif proxy_type == "tuic":
            outbound["uuid"] = _text(node, "uuid")
            outbound["password"] = _text(node, "password")
        else:
            outbound["password"] = _text(node, "password")
    else:
        raise KernelBridgeError(f"sing-box 不支持 Clash 节点类型：{proxy_type or '未知'}")

    if proxy_type in {"vless", "vmess", "trojan", "hysteria", "hysteria2", "tuic", "anytls"}:
        outbound["tls"] = _sing_box_tls(
            node,
            force=proxy_type in {"hysteria", "hysteria2", "tuic", "anytls"},
        )
    transport = _sing_box_transport(node)
    if transport:
        outbound["transport"] = transport
    return outbound


def build_sing_box_config(node: dict[str, Any], port: int) -> dict[str, Any]:
    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "listen_port": port,
            }
        ],
        "outbounds": [
            _sing_box_outbound(node),
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"final": "proxy-out", "auto_detect_interface": True},
    }


def build_mihomo_config(node: dict[str, Any], port: int) -> dict[str, Any]:
    node_copy = dict(node)
    name = _node_name(node_copy)
    node_copy["name"] = name
    return {
        "mixed-port": port,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": True,
        "proxies": [node_copy],
        "proxy-groups": [{"name": "proxy-out", "type": "select", "proxies": [name]}],
        "rules": ["MATCH,proxy-out"],
    }


@dataclass
class _RunningBridge:
    key: str
    kernel: str
    port: int
    process: subprocess.Popen[Any]
    workdir: Path
    log_path: Path
    last_used: float


class ProxyKernelBridgeManager:
    """Starts and reuses one local SOCKS bridge per node and kernel."""

    def __init__(self, kernel_manager: ProxyKernelManager | None = None) -> None:
        self.kernel_manager = kernel_manager or ProxyKernelManager()
        self._bridges: dict[str, _RunningBridge] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def work_root(self) -> Path:
        configured = get_config().get_str("proxy.kernels.work_directory", "").strip()
        if configured:
            path = Path(configured).expanduser()
            return path if path.is_absolute() else Path.cwd() / path
        return data_path("proxy-bridges")

    def statuses(self):
        return self.kernel_manager.statuses()

    def choose_kernel(
        self, candidate: ClashProxyCandidate, preferred: str | None = None
    ) -> str:
        normalized = normalize_kernel(preferred)
        supported = tuple(candidate.kernels)
        if normalized:
            if normalized not in supported:
                raise KernelBridgeError(
                    f"节点 {candidate.name} 不支持 {normalized} 内核"
                )
            return normalized
        if KERNEL_NATIVE in supported:
            return KERNEL_NATIVE
        for kernel in supported:
            if self.kernel_manager.status(kernel).installed:
                return kernel
        if supported:
            return supported[0]
        raise KernelBridgeError(f"节点 {candidate.name} 没有可用的代理内核")

    async def ensure_candidate(
        self,
        candidate: ClashProxyCandidate,
        preferred: str | None = None,
        *,
        auto_download: bool = False,
    ) -> tuple[str, str]:
        kernel = self.choose_kernel(candidate, preferred)
        if kernel == KERNEL_NATIVE:
            if not candidate.proxy_url:
                raise KernelBridgeError("原生代理 URL 为空")
            return candidate.proxy_url, kernel
        if not candidate.raw_node:
            raise KernelBridgeError("节点原始配置为空，无法生成内核配置")
        try:
            binary = await self.kernel_manager.ensure(kernel, auto_download=auto_download)
        except RuntimeError as exc:
            raise KernelBridgeError(str(exc)) from exc
        key = self._bridge_key(candidate, kernel)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            running = self._bridges.get(key)
            if running and await self._is_ready(running):
                running.last_used = time.time()
                return f"socks5://127.0.0.1:{running.port}", kernel
            if running:
                self._forget_bridge(key, running)
            try:
                bridge = await self._launch(
                    key,
                    kernel,
                    binary,
                    candidate.raw_node,
                )
            except Exception:
                raise
            self._bridges[key] = bridge
            return f"socks5://127.0.0.1:{bridge.port}", kernel

    def stop_candidate(self, candidate: ClashProxyCandidate, kernel: str | None = None) -> None:
        if kernel:
            keys = [self._bridge_key(candidate, normalize_kernel(kernel))]
        else:
            prefix = self._candidate_prefix(candidate)
            keys = [key for key in self._bridges if key.startswith(prefix)]
        for key in keys:
            bridge = self._bridges.pop(key, None)
            if bridge:
                self._terminate(bridge.process)

    def stop_all(self) -> None:
        bridges = list(self._bridges.values())
        self._bridges.clear()
        for bridge in bridges:
            self._terminate(bridge.process)

    def _candidate_prefix(self, candidate: ClashProxyCandidate) -> str:
        raw = candidate.raw_node or {
            "id": candidate.proxy_id,
            "name": candidate.name,
            "type": candidate.proxy_type,
            "server": candidate.server,
            "port": candidate.port,
        }
        digest = hashlib.sha256(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return f"{digest[:24]}-"

    def _bridge_key(self, candidate: ClashProxyCandidate, kernel: str) -> str:
        return f"{self._candidate_prefix(candidate)}{kernel}"

    async def _launch(
        self,
        key: str,
        kernel: str,
        binary: Path,
        node: dict[str, Any],
    ) -> _RunningBridge:
        port = self._free_port()
        workdir = self.work_root / kernel / key
        workdir.mkdir(parents=True, exist_ok=True)
        config_path = workdir / self._config_name(kernel)
        if kernel == KERNEL_XRAY:
            config = build_xray_config(node, port)
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        elif kernel == KERNEL_SING_BOX:
            config = build_sing_box_config(node, port)
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        elif kernel == KERNEL_MIHOMO:
            config = build_mihomo_config(node, port)
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
        else:
            raise KernelBridgeError(f"不支持的代理内核：{kernel}")
        try:
            config_path.chmod(0o600)
        except OSError:
            pass
        log_path = workdir / "bridge.log"
        command = self._command(kernel, binary, config_path, workdir)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            with log_path.open("ab") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=str(workdir),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
        except OSError as exc:
            raise KernelBridgeError(f"{kernel} 内核启动失败：{exc}") from exc
        bridge = _RunningBridge(key, kernel, port, process, workdir, log_path, time.time())
        try:
            await self._wait_ready(bridge)
        except Exception as exc:
            self._terminate(process)
            detail = self._log_tail(log_path)
            suffix = f"；日志：{detail}" if detail else ""
            raise KernelBridgeError(f"{kernel} 内核未就绪：{exc}{suffix}") from exc
        logger.info(
            "proxy kernel bridge started: kernel={} port={} pid={}",
            kernel,
            port,
            process.pid,
        )
        return bridge

    @staticmethod
    def _config_name(kernel: str) -> str:
        return {
            KERNEL_XRAY: "xray-config.json",
            KERNEL_SING_BOX: "singbox-config.json",
            KERNEL_MIHOMO: "mihomo-config.yaml",
        }[kernel]

    @staticmethod
    def _command(kernel: str, binary: Path, config: Path, workdir: Path) -> list[str]:
        if kernel == KERNEL_MIHOMO:
            return [str(binary), "-d", str(workdir), "-f", str(config)]
        return [str(binary), "run", "-c", str(config)]

    @staticmethod
    def _free_port() -> int:
        last_error: OSError | None = None
        exclusive_option = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        for _ in range(128):
            tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                if exclusive_option is not None:
                    tcp_sock.setsockopt(socket.SOL_SOCKET, exclusive_option, 1)
                    udp_sock.setsockopt(socket.SOL_SOCKET, exclusive_option, 1)
                tcp_sock.bind(("127.0.0.1", 0))
                port = int(tcp_sock.getsockname()[1])
                udp_sock.bind(("127.0.0.1", port))
                return port
            except OSError as exc:
                last_error = exc
            finally:
                tcp_sock.close()
                udp_sock.close()
        detail = f": {last_error}" if last_error else ""
        raise KernelBridgeError(
            f"无法分配同时支持 TCP/UDP 的本地代理端口{detail}"
        )

    async def _is_ready(self, bridge: _RunningBridge) -> bool:
        if bridge.process.poll() is not None:
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", bridge.port), timeout=0.5
            )
            del reader
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def _wait_ready(self, bridge: _RunningBridge) -> None:
        timeout = max(5.0, get_config().get_float("proxy.kernels.bridge_start_timeout_sec", 20.0))
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self._is_ready(bridge):
                return
            if bridge.process.poll() is not None:
                raise KernelBridgeError(f"进程已退出，返回码 {bridge.process.returncode}")
            await asyncio.sleep(0.1)
        raise KernelBridgeError(f"本地 SOCKS 端口 {bridge.port} 启动超时")

    def _forget_bridge(self, key: str, bridge: _RunningBridge) -> None:
        if self._bridges.get(key) is bridge:
            self._bridges.pop(key, None)
        self._terminate(bridge.process)

    @staticmethod
    def _terminate(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    @staticmethod
    def _log_tail(path: Path, limit: int = 2000) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip().replace("\n", " ")
        except OSError:
            return ""


_bridge_manager: ProxyKernelBridgeManager | None = None


def get_proxy_bridge_manager() -> ProxyKernelBridgeManager:
    global _bridge_manager
    if _bridge_manager is None:
        _bridge_manager = ProxyKernelBridgeManager()
        atexit.register(_bridge_manager.stop_all)
    return _bridge_manager


__all__ = [
    "KernelBridgeError",
    "ProxyKernelBridgeManager",
    "build_mihomo_config",
    "build_sing_box_config",
    "build_xray_config",
    "get_proxy_bridge_manager",
]
