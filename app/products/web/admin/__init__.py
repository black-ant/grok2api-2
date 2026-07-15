"""Admin API — router aggregator, shared DI, lightweight endpoints.

All admin endpoints live under ``/admin/api`` with ``verify_admin_key`` guard.
Heavy handlers are split into ``tokens`` and ``batch`` sub-modules.
"""

import re
import time
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import RootModel, ValidationError as PydanticValidationError

from app.control.account.backends.factory import get_repository_backend
from app.control.account.commands import ListAccountsQuery
from app.platform.auth.middleware import verify_admin_key
from app.platform.config.snapshot import config
from app.platform.errors import AppError, ErrorKind, ValidationError
from app.platform.logging.logger import logger, reload_file_logging
from app.platform.request_logging import request_log_store
from app.platform.storage import reconcile_local_media_cache_async

if TYPE_CHECKING:
    from app.control.account.refresh import AccountRefreshService
    from app.control.account.repository import AccountRepository

# ---------------------------------------------------------------------------
# Shared DI dependencies — inject via Depends, no try/except per call
# ---------------------------------------------------------------------------

_CFG_CHAR_REPLACEMENTS = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
        "\u2007": " ",
        "\u202f": " ",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\ufeff": "",
    }
)

_STARTUP_ONLY_CONFIG_PREFIXES = (
    "account.storage",
    "account.local",
    "account.redis",
    "account.mysql",
    "account.postgresql",
)


class ConfigPatchRequest(RootModel[dict[str, Any]]):
    """Loose config patch payload with explicit root typing."""


def _sanitize_text(value: Any, *, remove_all_spaces: bool = False) -> str:
    text = "" if value is None else str(value)
    text = text.translate(_CFG_CHAR_REPLACEMENTS)
    if remove_all_spaces:
        text = re.sub(r"\s+", "", text)
    else:
        text = text.strip()
    return text.encode("latin-1", errors="ignore").decode("latin-1")


def _sanitize_proxy_config(payload: dict[str, Any]) -> dict[str, Any]:
    proxy = payload.get("proxy")
    if not isinstance(proxy, dict):
        return dict(payload)

    sanitized = dict(proxy)
    changed = False

    def _sanitize_fields(target: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized = dict(target)
        local_changed = False
        for key, strip_spaces in [
            ("user_agent", False),
            ("cf_cookies", False),
            ("cf_clearance", True),
        ]:
            if key not in normalized:
                continue
            raw = normalized[key]
            val = _sanitize_text(raw, remove_all_spaces=strip_spaces)
            if val != raw:
                normalized[key] = val
                local_changed = True
        return normalized, local_changed

    sanitized, changed = _sanitize_fields(sanitized)

    clearance = sanitized.get("clearance")
    if isinstance(clearance, dict):
        sanitized_clearance, clearance_changed = _sanitize_fields(clearance)
        if clearance_changed:
            sanitized["clearance"] = sanitized_clearance
            changed = True

    if not changed:
        return dict(payload)

    logger.warning("admin config payload sanitized before save: section=proxy")
    result = dict(payload)
    result["proxy"] = sanitized
    return result


def _iter_patch_paths(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(child, dict):
                yield from _iter_patch_paths(child, path)
            else:
                yield path


def _ensure_runtime_patch_allowed(payload: dict[str, Any]) -> None:
    for path in _iter_patch_paths(payload):
        for blocked in _STARTUP_ONLY_CONFIG_PREFIXES:
            if path == blocked or path.startswith(f"{blocked}."):
                raise ValidationError(
                    "Storage config is startup-only and must be set via env",
                    param=path,
                    code="startup_only_config",
                )


def _patch_touches_prefix(payload: dict[str, Any], prefix: str) -> bool:
    return any(
        path == prefix or path.startswith(f"{prefix}.")
        for path in _iter_patch_paths(payload)
    )


def get_repo(request: Request) -> "AccountRepository":
    """Resolve the singleton AccountRepository from app state."""
    return request.app.state.repository


def get_refresh_svc(request: Request) -> "AccountRefreshService":
    """Resolve the singleton AccountRefreshService from app state."""
    return request.app.state.refresh_service


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/admin/api", dependencies=[Depends(verify_admin_key)])
_TAG_ADMIN_SYSTEM = "Admin - System"

# Mount sub-modules
from .tokens import router as _tokens_router  # noqa: E402
from .batch import router as _batch_router  # noqa: E402
from .assets import router as _assets_router  # noqa: E402
from .cache import router as _cache_router  # noqa: E402

router.include_router(_tokens_router)
router.include_router(_batch_router)
router.include_router(_assets_router)
router.include_router(_cache_router)


# ---------------------------------------------------------------------------
# Lightweight inline endpoints (no separate file needed)
# ---------------------------------------------------------------------------


@router.get("/verify", tags=[_TAG_ADMIN_SYSTEM])
async def admin_verify():
    return {"status": "success"}


@router.get("/config", tags=[_TAG_ADMIN_SYSTEM])
async def get_config_endpoint():
    return Response(
        content=orjson.dumps(config.raw()),
        media_type="application/json",
    )


@router.post("/config", tags=[_TAG_ADMIN_SYSTEM])
async def update_config(req: ConfigPatchRequest):
    from app.control.account.runtime import reconcile_refresh_runtime

    patch = _sanitize_proxy_config(req.root)
    _ensure_runtime_patch_allowed(patch)
    cache_local_changed = _patch_touches_prefix(patch, "cache.local")
    await config.update(patch)
    # config.update() only writes to the backend and invalidates the in-memory
    # snapshot (_version = None); it does not refresh the data.  load() is
    # required here so that get_str/get_int calls below return the new values.
    await config.load()
    reload_file_logging(
        file_level=config.get_str("logging.file_level", "") or None,
        max_files=config.get_int("logging.max_files", 7),
    )
    if cache_local_changed:
        await reconcile_local_media_cache_async()
    strategy_name = reconcile_refresh_runtime()
    return {
        "status": "success",
        "message": "配置已更新",
        "selection_strategy": strategy_name,
    }


@router.get("/storage", tags=[_TAG_ADMIN_SYSTEM])
async def get_storage_mode():
    return {"type": get_repository_backend()}


@router.get("/status", tags=[_TAG_ADMIN_SYSTEM])
async def runtime_status():
    from app.control.account.runtime import reconcile_refresh_runtime
    from app.dataplane.account import _directory

    if _directory is None:
        raise AppError(
            "Account directory not initialised",
            kind=ErrorKind.SERVER,
            code="directory_not_initialised",
            status=503,
        )
    strategy_name = reconcile_refresh_runtime()
    return Response(
        content=orjson.dumps(
            {
                "status": "ok",
                "size": _directory.size,
                "revision": _directory.revision,
                "selection_strategy": strategy_name,
            }
        ),
        media_type="application/json",
    )


@router.get("/request-logs", tags=[_TAG_ADMIN_SYSTEM])
async def get_request_logs(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    total = await request_log_store.count()
    return Response(
        content=orjson.dumps(
            {
                "retention_days": request_log_store.retention_days,
                "retained_dates": request_log_store.retained_dates(),
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": await request_log_store.list(limit=limit, offset=offset),
            }
        ),
        media_type="application/json",
    )


@router.get("/debug/chat/models", tags=[_TAG_ADMIN_SYSTEM])
async def list_debug_chat_models():
    from app.control.model import registry as model_registry

    models = [
        {
            "id": spec.model_name,
            "name": spec.public_name,
            "pool": spec.pool_name(),
            "console": spec.is_console_chat(),
        }
        for spec in model_registry.list_enabled()
        if spec.is_chat() or spec.is_console_chat()
    ]
    return Response(
        content=orjson.dumps({"object": "list", "data": models}),
        media_type="application/json",
    )


@router.get("/debug/chat/tokens", tags=[_TAG_ADMIN_SYSTEM])
async def list_debug_chat_tokens(repo: "AccountRepository" = Depends(get_repo)):
    all_items: list = []
    page_num = 1
    while True:
        page = await repo.list_accounts(ListAccountsQuery(page=page_num, page_size=2000))
        all_items.extend(page.items)
        if page_num * 2000 >= page.total:
            break
        page_num += 1

    tokens = [
        {
            "token": record.token,
            "label": f"{record.token[:8]}...{record.token[-8:]} · {record.pool or 'basic'} · {record.status}",
            "pool": record.pool or "basic",
            "status": record.status,
            "tags": record.tags or [],
        }
        for record in all_items
        if not getattr(record, "deleted_at", None)
    ]
    return Response(
        content=orjson.dumps({"object": "list", "data": tokens}),
        media_type="application/json",
    )


@router.post("/debug/chat", tags=[_TAG_ADMIN_SYSTEM])
async def debug_chat(req: Request):
    from app.products.openai.chat import completions as chat_completions
    from app.products.openai.router import _validate_chat
    from app.products.openai.schemas import ChatCompletionRequest

    try:
        payload = await req.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    try:
        force_token = str(payload.pop("token", "") or "").strip() or None
        chat_req = ChatCompletionRequest.model_validate(payload)
    except PydanticValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    _validate_chat(chat_req)

    started = time.perf_counter()
    messages = [m.model_dump(exclude_none=True) for m in chat_req.messages]
    result = await chat_completions(
        model=chat_req.model,
        messages=messages,
        stream=False,
        emit_think=None if chat_req.reasoning_effort is None else chat_req.reasoning_effort != "none",
        tools=chat_req.tools,
        tool_choice=chat_req.tool_choice,
        temperature=chat_req.temperature if chat_req.temperature is not None else 0.8,
        top_p=chat_req.top_p if chat_req.top_p is not None else 0.95,
        force_token=force_token,
    )
    return Response(
        content=orjson.dumps(
            {
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "request": {
                    "model": chat_req.model,
                    "messages": messages,
                    "stream": False,
                    "reasoning_effort": chat_req.reasoning_effort,
                    "temperature": chat_req.temperature,
                    "top_p": chat_req.top_p,
                    "tools": chat_req.tools,
                    "tool_choice": chat_req.tool_choice,
                    "token": force_token,
                },
                "response": result,
            }
        ),
        media_type="application/json",
    )


@router.post("/sync", tags=[_TAG_ADMIN_SYSTEM])
async def force_sync():
    from app.dataplane.account import _directory

    if _directory is None:
        raise AppError(
            "Account directory not initialised",
            kind=ErrorKind.SERVER,
            code="directory_not_initialised",
            status=503,
        )
    changed = await _directory.sync_if_changed()
    return Response(
        content=orjson.dumps({"changed": changed, "revision": _directory.revision}),
        media_type="application/json",
    )


__all__ = ["router", "get_repo", "get_refresh_svc"]
