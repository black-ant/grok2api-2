"""Global proxy management APIs."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.control.proxy.clash import (
    ClashParseError,
    find_clash_candidate,
    parse_clash_yaml,
)
from app.platform.config.snapshot import config
from app.platform.errors import ValidationError


router = APIRouter(prefix="/proxy", tags=["Admin - Proxy"])


class ClashParseRequest(BaseModel):
    yaml: str


class ClashApplyRequest(BaseModel):
    yaml: str | None = None
    proxy_id: str | None = None
    enabled: bool = True


def _state() -> dict[str, Any]:
    return {
        "scope": "global",
        "enabled": config.get_bool("proxy.clash.enabled", False),
        "yaml": config.get_str("proxy.clash.yaml", ""),
        "selected_proxy_id": config.get_str("proxy.clash.selected_proxy_id", ""),
        "selected_proxy_name": config.get_str("proxy.clash.selected_proxy_name", ""),
    }


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

    public_candidates = [candidate.public_dict() for candidate in candidates]
    return {
        "scope": "global",
        "proxies": public_candidates,
        "total": len(public_candidates),
        "supported": sum(1 for candidate in candidates if candidate.supported),
    }


@router.post("/clash")
async def apply_clash(req: ClashApplyRequest):
    if not req.enabled:
        await config.update({"proxy": {"clash": {"enabled": False}}})
        await config.load()
        return {"status": "success", **_state()}

    raw_yaml = (
        req.yaml if req.yaml is not None else config.get_str("proxy.clash.yaml", "")
    )
    try:
        candidate = find_clash_candidate(raw_yaml, req.proxy_id or "")
    except ClashParseError as exc:
        param = "proxy_id" if req.proxy_id else "yaml"
        raise _parse_error(exc, param=param) from exc

    patch = {
        "proxy": {
            "clash": {
                "enabled": True,
                "yaml": raw_yaml.strip(),
                "selected_proxy_id": candidate.proxy_id,
                "selected_proxy_name": candidate.name,
                "selected_url": candidate.proxy_url,
            }
        }
    }
    await config.update(patch)
    await config.load()
    return {"status": "success", **_state()}


__all__ = ["router"]
