"""Global proxy management APIs."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.control.proxy.clash import (
    ClashParseError,
    find_clash_candidate,
    parse_clash_yaml,
)
from app.control.proxy.bridge import KernelBridgeError, get_proxy_bridge_manager
from app.control.proxy.kernels import normalize_kernel
from app.platform.config.snapshot import config
from app.platform.errors import ValidationError


router = APIRouter(prefix="/proxy", tags=["Admin - Proxy"])


class ClashParseRequest(BaseModel):
    yaml: str


class ClashApplyRequest(BaseModel):
    yaml: str | None = None
    proxy_id: str | None = None
    selected_kernel: str | None = None
    enabled: bool = True


def _state() -> dict[str, Any]:
    bridge_manager = get_proxy_bridge_manager()
    raw_yaml = _draft_yaml()
    try:
        candidates = parse_clash_yaml(raw_yaml) if raw_yaml else []
    except ClashParseError:
        candidates = []
    return {
        "scope": "global",
        "enabled": config.get_bool("proxy.clash.enabled", False),
        "yaml": raw_yaml,
        "selected_proxy_id": config.get_str("proxy.clash.selected_proxy_id", ""),
        "selected_proxy_name": config.get_str("proxy.clash.selected_proxy_name", ""),
        "selected_kernel": config.get_str("proxy.clash.selected_kernel", "auto"),
        "proxies": [candidate.public_dict() for candidate in candidates],
        "total": len(candidates),
        "supported": sum(1 for candidate in candidates if candidate.supported),
        "kernels": [status.public_dict() for status in bridge_manager.statuses()],
    }


def _draft_yaml() -> str:
    return (
        config.get_str("proxy.clash.draft_yaml", "").strip()
        or config.get_str("proxy.clash.yaml", "").strip()
    )


def _parse_error(exc: ClashParseError, *, param: str) -> ValidationError:
    return ValidationError(str(exc), param=param, code="invalid_clash_config")


@router.get("/clash")
async def get_clash_state():
    return _state()


@router.post("/clash/parse")
async def parse_clash(req: ClashParseRequest):
    try:
        candidates = parse_clash_yaml(req.yaml)
    except ClashParseError as exc:
        raise _parse_error(exc, param="yaml") from exc

    raw_yaml = req.yaml.strip()
    await config.update({"proxy": {"clash": {"draft_yaml": raw_yaml}}})
    await config.load()
    public_candidates = [candidate.public_dict() for candidate in candidates]
    return {
        "scope": "global",
        "yaml": raw_yaml,
        "proxies": public_candidates,
        "total": len(public_candidates),
        "supported": sum(1 for candidate in candidates if candidate.supported),
    }


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

    raw_yaml = (
        req.yaml if req.yaml is not None else _draft_yaml()
    )
    try:
        candidate = find_clash_candidate(raw_yaml, req.proxy_id or "")
    except ClashParseError as exc:
        param = "proxy_id" if req.proxy_id else "yaml"
        raise _parse_error(exc, param=param) from exc

    bridge_manager = get_proxy_bridge_manager()
    preferred_kernel = normalize_kernel(req.selected_kernel)
    try:
        selected_url, selected_kernel = await bridge_manager.ensure_candidate(
            candidate,
            preferred_kernel,
            auto_download=config.get_bool("proxy.kernels.auto_download", False),
        )
    except (KernelBridgeError, RuntimeError) as exc:
        raise ValidationError(
            str(exc), param="selected_kernel", code="kernel_not_ready"
        ) from exc

    patch = {
        "proxy": {
            "clash": {
                "enabled": True,
                "yaml": raw_yaml.strip(),
                "draft_yaml": raw_yaml.strip(),
                "selected_proxy_id": candidate.proxy_id,
                "selected_proxy_name": candidate.name,
                "selected_kernel": selected_kernel,
                "selected_url": selected_url,
            }
        }
    }
    await config.update(patch)
    await config.load()
    return {"status": "success", **_state()}


__all__ = ["router"]
