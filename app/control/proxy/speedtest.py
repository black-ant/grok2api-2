"""Connectivity checks for parsed Clash proxy candidates."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from app.platform.config.snapshot import get_config

from .bridge import KernelBridgeError, ProxyKernelBridgeManager
from .clash import ClashProxyCandidate
from .kernels import normalize_kernel


DEFAULT_TEST_URL = "https://api.ipify.org?format=json"


def _proxy_kwargs(proxy_url: str) -> dict[str, Any]:
    scheme = urlparse(proxy_url).scheme.lower()
    if scheme == "socks":
        proxy_url = "socks5h://" + proxy_url[len("socks://") :]
        scheme = "socks5h"
    elif scheme == "socks5":
        proxy_url = "socks5h://" + proxy_url[len("socks5://") :]
        scheme = "socks5h"
    elif scheme == "socks4":
        proxy_url = "socks4a://" + proxy_url[len("socks4://") :]
        scheme = "socks4a"
    if scheme.startswith("socks"):
        return {"proxy": proxy_url}
    return {"proxies": {"http": proxy_url, "https": proxy_url}}


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "连接超时"
    if isinstance(exc, KernelBridgeError):
        return "代理内核未准备"
    return "连接失败"


def _response_egress_ip(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return ''
    if not isinstance(payload, dict):
        return ''
    value = str(payload.get('ip') or '').strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ''


async def test_clash_candidate(
    candidate: ClashProxyCandidate,
    bridge_manager: ProxyKernelBridgeManager,
    *,
    target_url: str,
    timeout_sec: float,
    preferred_kernel: str | None = None,
    auto_download: bool = False,
    keep_bridge: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    selected_kernel = (
        "native"
        if candidate.kernels == ("native",)
        else normalize_kernel(preferred_kernel) or "auto"
    )
    actual_kernel = selected_kernel
    try:
        proxy_url, actual_kernel = await bridge_manager.ensure_candidate(
            candidate,
            selected_kernel,
            auto_download=auto_download,
        )
        from curl_cffi import requests as curl_requests

        async with curl_requests.AsyncSession(**_proxy_kwargs(proxy_url)) as session:
            response = await session.get(
                target_url,
                timeout=timeout_sec,
                allow_redirects=False,
            )
        latency_ms = max(1, round((time.perf_counter() - started) * 1000))
        egress_ip = _response_egress_ip(response)
        return {
            'egress_ip': egress_ip,
            "state": "alive",
            "latency_ms": latency_ms,
            "status_code": int(response.status_code),
            "kernel": actual_kernel,
            "error": "",
            "tested_at": int(time.time() * 1000),
        }
    except KernelBridgeError as exc:
        return {
            "state": "unavailable",
            "latency_ms": None,
            "status_code": None,
            "kernel": actual_kernel,
            "error": str(exc) or "代理内核未准备",
            "tested_at": int(time.time() * 1000),
        }
    except Exception as exc:
        return {
            "state": "dead",
            "latency_ms": None,
            "status_code": None,
            "kernel": actual_kernel,
            "error": _error_text(exc),
            "tested_at": int(time.time() * 1000),
        }
    finally:
        if not keep_bridge and actual_kernel not in {"auto", "native"}:
            bridge_manager.stop_candidate(candidate, actual_kernel)


async def iter_clash_candidate_results(
    candidates: list[ClashProxyCandidate],
    bridge_manager: ProxyKernelBridgeManager,
    *,
    target_url: str = DEFAULT_TEST_URL,
    timeout_sec: float = 8.0,
    preferred_kernel: str | None = None,
    preferred_kernels: dict[str, str] | None = None,
    auto_download: bool = False,
    keep_proxy_ids: set[str] | None = None,
    concurrency: int = 4,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    keep_proxy_ids = keep_proxy_ids or set()
    preferred_kernels = preferred_kernels or {}

    async def _test(candidate: ClashProxyCandidate) -> tuple[str, dict[str, Any]]:
        if not candidate.supported:
            return candidate.proxy_id, {
                "state": "unsupported",
                "latency_ms": None,
                "status_code": None,
                "kernel": "",
                "error": candidate.reason or "节点不可用",
                "tested_at": int(time.time() * 1000),
            }
        async with semaphore:
            result = await test_clash_candidate(
                candidate,
                bridge_manager,
                target_url=target_url,
                timeout_sec=timeout_sec,
                preferred_kernel=preferred_kernels.get(candidate.proxy_id) or preferred_kernel,
                auto_download=auto_download,
                keep_bridge=candidate.proxy_id in keep_proxy_ids,
            )
        return candidate.proxy_id, result

    tasks = [asyncio.create_task(_test(candidate)) for candidate in candidates]
    try:
        for task in asyncio.as_completed(tasks):
            yield await task
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def test_clash_candidates(
    candidates: list[ClashProxyCandidate],
    bridge_manager: ProxyKernelBridgeManager,
    *,
    target_url: str = DEFAULT_TEST_URL,
    timeout_sec: float = 8.0,
    preferred_kernel: str | None = None,
    preferred_kernels: dict[str, str] | None = None,
    auto_download: bool = False,
    keep_proxy_ids: set[str] | None = None,
    concurrency: int = 4,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    async for proxy_id, result in iter_clash_candidate_results(
        candidates,
        bridge_manager,
        target_url=target_url,
        timeout_sec=timeout_sec,
        preferred_kernel=preferred_kernel,
        preferred_kernels=preferred_kernels,
        auto_download=auto_download,
        keep_proxy_ids=keep_proxy_ids,
        concurrency=concurrency,
    ):
        results[proxy_id] = result
    return results


def speedtest_config() -> tuple[str, float, bool, int]:
    cfg = get_config()
    target_url = cfg.get_str("proxy.clash.test_url", DEFAULT_TEST_URL).strip()
    timeout_sec = max(1.0, min(60.0, cfg.get_float("proxy.clash.test_timeout_sec", 8.0)))
    auto_download = cfg.get_bool("proxy.kernels.auto_download", True)
    concurrency = max(1, min(8, cfg.get_int("proxy.clash.test_concurrency", 4)))
    return target_url or DEFAULT_TEST_URL, timeout_sec, auto_download, concurrency


__all__ = [
    "DEFAULT_TEST_URL",
    "iter_clash_candidate_results",
    "speedtest_config",
    "test_clash_candidate",
    "test_clash_candidates",
]
