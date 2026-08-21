"""Global proxy management APIs."""

import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.control.proxy.clash import (
    ClashParseError,
    parse_clash_yaml,
)
from app.control.proxy.bridge import KernelBridgeError, get_proxy_bridge_manager
from app.control.proxy.kernels import normalize_kernel
from app.control.proxy.speedtest import (
    iter_clash_candidate_results,
    speedtest_config,
    test_clash_candidates,
)
from app.platform.config.snapshot import config
from app.platform.errors import ValidationError


router = APIRouter(prefix="/proxy", tags=["Admin - Proxy"])


class ClashParseRequest(BaseModel):
    yaml: str


class ClashApplyRequest(BaseModel):
    yaml: str | None = None
    proxy_id: str | None = None
    proxy_ids: list[str] | None = None
    selected_kernel: str | None = None
    pool_kernels: dict[str, str] | None = None
    enabled: bool = True


class ClashSpeedTestRequest(BaseModel):
    yaml: str | None = None
    proxy_ids: list[str] | None = None
    selected_kernel: str | None = None
    formal_pool: bool = False


def _state() -> dict[str, Any]:
    bridge_manager = get_proxy_bridge_manager()
    raw_yaml = _draft_yaml()
    committed_yaml = config.get_str("proxy.clash.yaml", "").strip()
    pool_proxy_ids = _pool_proxy_ids()
    pool_kernels = _pool_kernels()
    speed_results = _speed_results()
    public_candidates = _public_candidates(
        raw_yaml,
        pool_proxy_ids,
        pool_kernels,
        speed_results,
    )
    committed_candidates = _public_candidates(
        committed_yaml,
        pool_proxy_ids,
        pool_kernels,
        speed_results,
    )
    return {
        "scope": "global",
        "enabled": config.get_bool("proxy.clash.enabled", False),
        "yaml": raw_yaml,
        "committed_yaml": committed_yaml,
        "selected_proxy_id": config.get_str("proxy.clash.selected_proxy_id", ""),
        "selected_proxy_name": config.get_str("proxy.clash.selected_proxy_name", ""),
        "selected_kernel": config.get_str("proxy.clash.selected_kernel", "auto"),
        "pool_proxy_ids": pool_proxy_ids,
        "pool_kernels": pool_kernels,
        "pool_size": len(pool_proxy_ids),
        "proxies": public_candidates,
        "committed_proxies": committed_candidates,
        "pool_proxies": [candidate for candidate in committed_candidates if candidate["in_pool"]],
        "total": len(public_candidates),
        "supported": sum(1 for candidate in public_candidates if candidate["supported"]),
        "kernels": [status.public_dict() for status in bridge_manager.statuses()],
    }


def _draft_yaml() -> str:
    return (
        config.get_str("proxy.clash.draft_yaml", "").strip()
        or config.get_str("proxy.clash.yaml", "").strip()
    )


def _unique_ids(values: list[str] | tuple[str, ...] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        proxy_id = str(value).strip()
        if proxy_id and proxy_id not in result:
            result.append(proxy_id)
    return result


def _pool_proxy_ids() -> list[str]:
    configured = _unique_ids(config.get_list("proxy.clash.pool_proxy_ids", []))
    if configured:
        return configured
    legacy = config.get_str("proxy.clash.selected_proxy_id", "").strip()
    return [legacy] if legacy else []


def _pool_kernels() -> dict[str, str]:
    getter = getattr(config, "get", None)
    value = getter("proxy.clash.pool_kernels", {}) if getter else {}
    if not isinstance(value, dict):
        return {}
    return {
        str(proxy_id): str(kernel).strip()
        for proxy_id, kernel in value.items()
        if str(proxy_id).strip() and str(kernel).strip()
    }


def _public_candidates(
    raw_yaml: str,
    pool_proxy_ids: list[str],
    pool_kernels: dict[str, str],
    speed_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not raw_yaml:
        return []
    try:
        candidates = parse_clash_yaml(raw_yaml)
    except ClashParseError:
        return []
    public_candidates = []
    for candidate in candidates:
        public = candidate.public_dict()
        public["in_pool"] = candidate.proxy_id in pool_proxy_ids
        public["pool_kernel"] = pool_kernels.get(candidate.proxy_id, "")
        public["speed"] = speed_results.get(candidate.proxy_id, {})
        public_candidates.append(public)
    return public_candidates


def _speed_results() -> dict[str, dict[str, Any]]:
    getter = getattr(config, "get", None)
    value = getter("proxy.clash.speed_results", {}) if getter else {}
    if not isinstance(value, dict):
        return {}
    return {
        str(proxy_id): result
        for proxy_id, result in value.items()
        if str(proxy_id).strip() and isinstance(result, dict)
    }


def _persistable_speed_results(
    results: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    persisted: dict[str, dict[str, Any]] = {}
    for proxy_id, result in results.items():
        safe_result = dict(result)
        if safe_result.get("latency_ms") is None:
            safe_result["latency_ms"] = 0
        if safe_result.get("status_code") is None:
            safe_result["status_code"] = 0
        persisted[str(proxy_id)] = {
            str(key): value
            for key, value in safe_result.items()
            if value is not None
        }
    return persisted


def _request_proxy_ids(req: ClashApplyRequest) -> list[str]:
    if req.proxy_ids is not None:
        return _unique_ids(req.proxy_ids)
    if req.proxy_id:
        return _unique_ids([req.proxy_id])
    return _pool_proxy_ids()


def _parse_error(exc: ClashParseError, *, param: str) -> ValidationError:
    return ValidationError(str(exc), param=param, code="invalid_clash_config")


async def _prepare_speed_test(req: ClashSpeedTestRequest) -> dict[str, Any]:
    raw_yaml = (
        config.get_str("proxy.clash.yaml", "").strip()
        if req.formal_pool
        else req.yaml.strip()
        if req.yaml is not None
        else _draft_yaml()
    )
    try:
        candidates = parse_clash_yaml(raw_yaml)
    except ClashParseError as exc:
        raise _parse_error(exc, param="yaml") from exc
    if req.yaml is not None and not req.formal_pool:
        await config.update({"proxy": {"clash": {"draft_yaml": raw_yaml}}})
        await config.load()

    candidates_by_id = {candidate.proxy_id: candidate for candidate in candidates}
    proxy_ids = _unique_ids(req.proxy_ids)
    configured_pool_ids = _pool_proxy_ids()
    if req.formal_pool and not proxy_ids:
        proxy_ids = configured_pool_ids
    elif not proxy_ids:
        proxy_ids = [candidate.proxy_id for candidate in candidates if candidate.supported]
    missing = [proxy_id for proxy_id in proxy_ids if proxy_id not in candidates_by_id]
    if missing:
        raise ValidationError("测速节点不在当前 YAML 中", param="proxy_ids")
    if req.formal_pool:
        outside_pool = [proxy_id for proxy_id in proxy_ids if proxy_id not in configured_pool_ids]
        if outside_pool:
            raise ValidationError("测速节点不在正式代理池中", param="proxy_ids")

    target_url, timeout_sec, auto_download, concurrency = speedtest_config()
    configured_kernels = _pool_kernels() if req.formal_pool else {}
    return {
        "candidates": [candidates_by_id[proxy_id] for proxy_id in proxy_ids],
        "proxy_ids": proxy_ids,
        "bridge_manager": get_proxy_bridge_manager(),
        "target_url": target_url,
        "timeout_sec": timeout_sec,
        "auto_download": auto_download,
        "preferred_kernel": normalize_kernel(req.selected_kernel),
        "preferred_kernels": {
            proxy_id: normalize_kernel(configured_kernels.get(proxy_id))
            for proxy_id in proxy_ids
            if normalize_kernel(configured_kernels.get(proxy_id))
        },
        "keep_proxy_ids": set(configured_pool_ids),
        "concurrency": concurrency,
        "formal_pool": req.formal_pool,
    }


async def _persist_speed_results(results: dict[str, dict[str, Any]]) -> None:
    await config.update(
        {
            "proxy": {
                "clash": {
                    "speed_results": _persistable_speed_results(results),
                }
            }
        }
    )


async def _prune_failed_pool_nodes(
    prepared: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> list[str]:
    if not prepared["formal_pool"]:
        return []

    candidates_by_id = {
        candidate.proxy_id: candidate for candidate in prepared["candidates"]
    }
    current_ids = _pool_proxy_ids()
    failed_ids = {
        proxy_id
        for proxy_id in prepared["proxy_ids"]
        if results.get(proxy_id, {}).get("state") != "alive"
    }
    removed_ids = [proxy_id for proxy_id in current_ids if proxy_id in failed_ids]
    if not removed_ids:
        return []

    bridge_manager = prepared["bridge_manager"]
    for proxy_id in removed_ids:
        candidate = candidates_by_id.get(proxy_id)
        if candidate is not None:
            bridge_manager.stop_candidate(candidate)

    remaining_ids = [proxy_id for proxy_id in current_ids if proxy_id not in failed_ids]
    configured_kernels = _pool_kernels()
    remaining_kernels = {
        proxy_id: configured_kernels[proxy_id]
        for proxy_id in remaining_ids
        if proxy_id in configured_kernels
    }
    current_selected_id = config.get_str("proxy.clash.selected_proxy_id", "").strip()
    current_selected_name = config.get_str("proxy.clash.selected_proxy_name", "").strip()
    current_selected_kernel = config.get_str("proxy.clash.selected_kernel", "auto").strip()
    selected_proxy_id = current_selected_id
    if selected_proxy_id not in remaining_ids:
        selected_proxy_id = remaining_ids[0] if remaining_ids else ""
    selected_candidate = candidates_by_id.get(selected_proxy_id)
    if not selected_proxy_id:
        selected_name = ""
        selected_kernel = "auto"
        selected_url = ""
    else:
        selected_name = selected_candidate.name if selected_candidate else current_selected_name
        selected_kernel = remaining_kernels.get(selected_proxy_id, current_selected_kernel or "auto")
        selected_url = (
            config.get_str("proxy.clash.selected_url", "")
            if selected_proxy_id == current_selected_id
            else ""
        )
    patch = {
        "proxy": {
            "clash": {
                "enabled": config.get_bool("proxy.clash.enabled", False)
                and bool(remaining_ids),
                "pool_proxy_ids": remaining_ids,
                "pool_kernels": remaining_kernels,
                "selected_proxy_id": selected_proxy_id,
                "selected_proxy_name": selected_name,
                "selected_kernel": selected_kernel,
                "selected_url": selected_url,
            }
        }
    }
    await config.update(patch)
    await config.load()
    return removed_ids


def _sse_event(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {body}\n\n"


@router.get("/clash")
async def get_clash_state():
    return _state()


@router.post("/clash/parse")
async def parse_clash(req: ClashParseRequest):
    try:
        parse_clash_yaml(req.yaml)
    except ClashParseError as exc:
        raise _parse_error(exc, param="yaml") from exc

    raw_yaml = req.yaml.strip()
    await config.update({"proxy": {"clash": {"draft_yaml": raw_yaml}}})
    await config.load()
    return {"status": "success", **_state()}


@router.post("/clash/test")
async def test_clash(req: ClashSpeedTestRequest):
    prepared = await _prepare_speed_test(req)
    results = await test_clash_candidates(
        prepared["candidates"],
        prepared["bridge_manager"],
        target_url=prepared["target_url"],
        timeout_sec=prepared["timeout_sec"],
        preferred_kernel=prepared["preferred_kernel"],
        preferred_kernels=prepared["preferred_kernels"],
        auto_download=prepared["auto_download"],
        keep_proxy_ids=prepared["keep_proxy_ids"],
        concurrency=prepared["concurrency"],
    )
    merged_results = _speed_results()
    merged_results.update(results)
    await _persist_speed_results(merged_results)
    removed_ids = await _prune_failed_pool_nodes(prepared, results)
    await config.load()
    return {
        "status": "success",
        "results": results,
        "removed_proxy_ids": removed_ids,
        **_state(),
    }


@router.post("/clash/test/stream")
async def stream_clash_test(req: ClashSpeedTestRequest):
    prepared = await _prepare_speed_test(req)
    candidates = prepared["candidates"]

    async def _stream():
        results: dict[str, dict[str, Any]] = {}
        merged_results = _speed_results()
        total = len(candidates)
        yield _sse_event({"type": "started", "total": total})
        try:
            async for proxy_id, result in iter_clash_candidate_results(
                candidates,
                prepared["bridge_manager"],
                target_url=prepared["target_url"],
                timeout_sec=prepared["timeout_sec"],
                preferred_kernel=prepared["preferred_kernel"],
                preferred_kernels=prepared["preferred_kernels"],
                auto_download=prepared["auto_download"],
                keep_proxy_ids=prepared["keep_proxy_ids"],
                concurrency=prepared["concurrency"],
            ):
                results[proxy_id] = result
                merged_results[proxy_id] = result
                yield _sse_event(
                    {
                        "type": "result",
                        "proxy_id": proxy_id,
                        "result": result,
                        "completed": len(results),
                        "total": total,
                    }
                )
                await _persist_speed_results(merged_results)
            removed_ids = await _prune_failed_pool_nodes(prepared, results)
            await config.load()
            yield _sse_event(
                {
                    "type": "done",
                    "results": results,
                    "removed_proxy_ids": removed_ids,
                    "state": _state(),
                }
            )
        except Exception as exc:
            yield _sse_event(
                {"type": "error", "message": str(exc) or "测速失败"}
            )

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/clash/kernels/{kernel}/download")
async def download_clash_kernel(kernel: str):
    normalized = normalize_kernel(kernel)
    if not normalized or normalized == "native":
        raise ValidationError("native 不需要下载代理内核", param="kernel")
    manager = get_proxy_bridge_manager()
    try:
        status = await manager.kernel_manager.download(normalized)
    except (KernelBridgeError, RuntimeError) as exc:
        raise ValidationError(str(exc), param="kernel", code="kernel_download_failed") from exc
    return {"status": "success", "kernel": status.public_dict()}


@router.post("/clash")
async def apply_clash(req: ClashApplyRequest):
    if not req.enabled:
        get_proxy_bridge_manager().stop_all()
        await config.update({"proxy": {"clash": {"enabled": False}}})
        await config.load()
        return {"status": "success", **_state()}

    raw_yaml = req.yaml if req.yaml is not None else _draft_yaml()
    try:
        candidates = parse_clash_yaml(raw_yaml)
    except ClashParseError as exc:
        raise _parse_error(exc, param="yaml") from exc

    candidates_by_id = {candidate.proxy_id: candidate for candidate in candidates}
    requested_ids = _request_proxy_ids(req)
    if not requested_ids:
        raise ValidationError("请选择至少一个节点加入代理池", param="proxy_ids")
    missing = [proxy_id for proxy_id in requested_ids if proxy_id not in candidates_by_id]
    if missing:
        raise ValidationError("所选节点不在当前 YAML 中", param="proxy_ids")

    pool_ids = [
        proxy_id
        for proxy_id in _pool_proxy_ids()
        if proxy_id in candidates_by_id
    ]
    pool_ids = _unique_ids(pool_ids + requested_ids)
    unsupported = [
        candidates_by_id[proxy_id]
        for proxy_id in pool_ids
        if not candidates_by_id[proxy_id].supported
    ]
    if unsupported:
        candidate = unsupported[0]
        raise ValidationError(
            candidate.reason or "所选节点不可用",
            param="proxy_ids",
            code="invalid_clash_config",
        )

    bridge_manager = get_proxy_bridge_manager()
    preferred_kernel = normalize_kernel(req.selected_kernel)
    requested_kernels = req.pool_kernels or {}
    configured_kernels = _pool_kernels()
    actual_kernels: dict[str, str] = {}
    selected_urls: dict[str, str] = {}
    try:
        for proxy_id in pool_ids:
            candidate = candidates_by_id[proxy_id]
            if candidate.kernels == ("native",):
                node_kernel = "native"
            else:
                node_kernel = normalize_kernel(requested_kernels.get(proxy_id))
                if not node_kernel:
                    node_kernel = normalize_kernel(configured_kernels.get(proxy_id))
                if not node_kernel:
                    node_kernel = preferred_kernel
            selected_url, selected_kernel = await bridge_manager.ensure_candidate(
                candidate,
                node_kernel,
                auto_download=config.get_bool("proxy.kernels.auto_download", True),
            )
            selected_urls[proxy_id] = selected_url
            actual_kernels[proxy_id] = selected_kernel
    except (KernelBridgeError, RuntimeError) as exc:
        raise ValidationError(
            str(exc), param="selected_kernel", code="kernel_not_ready"
        ) from exc

    selected_proxy_id = requested_ids[-1]
    selected_name = (
        candidates_by_id[selected_proxy_id].name
        if len(pool_ids) == 1
        else f"代理池（{len(pool_ids)} 个节点）"
    )
    patch = {
        "proxy": {
            "clash": {
                "enabled": True,
                "yaml": raw_yaml.strip(),
                "draft_yaml": raw_yaml.strip(),
                "selected_proxy_id": selected_proxy_id,
                "selected_proxy_name": selected_name,
                "selected_kernel": actual_kernels[selected_proxy_id],
                "selected_url": selected_urls[pool_ids[0]],
                "pool_proxy_ids": pool_ids,
                "pool_kernels": actual_kernels,
            }
        }
    }
    await config.update(patch)
    await config.load()
    return {"status": "success", **_state()}


__all__ = ["router"]
