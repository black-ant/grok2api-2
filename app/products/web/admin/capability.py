"""Admin Grok account capability detection endpoints."""

from __future__ import annotations

import time
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.control.account.commands import ListAccountsQuery
from app.control.model import registry as model_registry
from app.control.model.enums import ModeId
from app.dataplane.reverse.protocol.xai_chat import StreamAdapter, classify_line
from app.dataplane.reverse.protocol.xai_console_chat import (
    ConsoleStreamAdapter,
    build_console_payload,
    stream_console_chat,
)
from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, ErrorKind, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.products.openai.chat import _stream_chat

from . import get_repo

if TYPE_CHECKING:
    from app.control.account.models import AccountRecord
    from app.control.account.repository import AccountRepository
    from app.control.model.spec import ModelSpec


router = APIRouter(prefix="/grok-capability", tags=["Admin - Grok Capability"])

_PAGE_SIZE = 2000
_MAX_MODELS_PER_SCAN = 40
_SCAN_MESSAGE = "Reply exactly: OK"


class CapabilityScanRequest(BaseModel):
    token_id: str = Field(min_length=16)
    models: list[str] = Field(default_factory=list)


def _token_id(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def _mask_token(token: str) -> str:
    if len(token) <= 20:
        return token
    return f"{token[:8]}...{token[-8:]}"


def _account_payload(record: "AccountRecord") -> dict[str, Any]:
    return {
        "id": _token_id(record.token),
        "label": f"{_mask_token(record.token)} · {record.pool or 'basic'} · {record.status}",
        "pool": record.pool or "basic",
        "status": str(record.status),
        "tags": record.tags or [],
    }


def _model_kind(spec: "ModelSpec") -> str:
    return "console" if spec.is_console_chat() else "chat"


def _model_payload(spec: "ModelSpec") -> dict[str, Any]:
    return {
        "id": spec.model_name,
        "name": spec.public_name,
        "kind": _model_kind(spec),
        "pool": spec.pool_name(),
        "mode_id": int(spec.mode_id),
    }


def _scan_models() -> list["ModelSpec"]:
    return [
        spec
        for spec in model_registry.list_enabled()
        if spec.is_chat() or spec.is_console_chat()
    ]


async def _list_accounts(repo: "AccountRepository") -> list["AccountRecord"]:
    items: list["AccountRecord"] = []
    page_num = 1
    while True:
        page = await repo.list_accounts(
            ListAccountsQuery(page=page_num, page_size=_PAGE_SIZE)
        )
        items.extend(page.items)
        if page_num * _PAGE_SIZE >= page.total:
            break
        page_num += 1
    return [record for record in items if not getattr(record, "deleted_at", None)]


async def _find_account(
    repo: "AccountRepository",
    token_id: str,
) -> "AccountRecord | None":
    page_num = 1
    while True:
        page = await repo.list_accounts(
            ListAccountsQuery(page=page_num, page_size=_PAGE_SIZE)
        )
        for record in page.items:
            if getattr(record, "deleted_at", None):
                continue
            if _token_id(record.token) == token_id:
                return record
        if page_num * _PAGE_SIZE >= page.total:
            break
        page_num += 1
    return None


def _preview_text(value: str, limit: int = 240) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _classify_error(status: int) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "unavailable"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "unavailable"
    return "unknown"


def _error_payload(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, AppError):
        body = str(exc.details.get("body") or "")[:400]
        return {
            "status": _classify_error(exc.status),
            "http_status": exc.status,
            "error_type": str(exc.kind),
            "error_code": exc.code,
            "message": exc.message,
            "body_preview": body,
        }
    return {
        "status": "unknown",
        "http_status": 0,
        "error_type": ErrorKind.SERVER,
        "error_code": "internal_error",
        "message": str(exc),
        "body_preview": "",
    }


async def _probe_console_model(token: str, model_id: str, timeout_s: float) -> str:
    adapter = ConsoleStreamAdapter()
    parts: list[str] = []
    payload = build_console_payload(
        messages=[{"role": "user", "content": _SCAN_MESSAGE}],
        model=model_id,
        temperature=0.0,
        top_p=1.0,
        reasoning_effort="none",
        stream=True,
    )
    async for event_type, data in stream_console_chat(token, payload, timeout_s=timeout_s):
        for token_text in adapter.feed(event_type, data):
            parts.append(token_text)
    return "".join(parts)


async def _probe_chat_model(token: str, spec: "ModelSpec", timeout_s: float) -> str:
    adapter = StreamAdapter()
    parts: list[str] = []
    try:
        mode_id = ModeId(int(spec.mode_id))
    except ValueError as exc:
        raise UpstreamError(f"Unsupported chat mode {int(spec.mode_id)}", status=400) from exc

    async for line in _stream_chat(
        token=token,
        mode_id=mode_id,
        message=_SCAN_MESSAGE,
        files=[],
        timeout_s=timeout_s,
    ):
        event_type, data = classify_line(line)
        if event_type == "done":
            break
        if event_type != "data" or not data:
            continue
        for event in adapter.feed(data):
            if event.kind == "text":
                parts.append(event.content)
            elif event.kind == "soft_stop":
                return "".join(parts)
    return "".join(parts)


@router.get("/options")
async def capability_options(repo: "AccountRepository" = Depends(get_repo)):
    accounts = await _list_accounts(repo)
    models = _scan_models()
    return Response(
        content=orjson.dumps(
            {
                "object": "grok_capability_options",
                "accounts": [_account_payload(record) for record in accounts],
                "models": [_model_payload(spec) for spec in models],
            }
        ),
        media_type="application/json",
    )


@router.post("/scan")
async def scan_capabilities(
    req: CapabilityScanRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    account = await _find_account(repo, req.token_id)
    if account is None:
        raise ValidationError("Grok token not found", param="token_id")

    model_by_id = {spec.model_name: spec for spec in _scan_models()}
    requested_models: list[str] = []
    for raw_model in req.models or list(model_by_id):
        model_id = str(raw_model or "").strip()
        if model_id and model_id not in requested_models:
            requested_models.append(model_id)

    if not requested_models:
        raise ValidationError("models cannot be empty", param="models")
    if len(requested_models) > _MAX_MODELS_PER_SCAN:
        raise ValidationError(
            f"models cannot exceed {_MAX_MODELS_PER_SCAN}",
            param="models",
        )

    invalid = [model_id for model_id in requested_models if model_id not in model_by_id]
    if invalid:
        raise ValidationError(
            f"Unsupported scan model: {invalid[0]}",
            param="models",
        )

    timeout_s = get_config().get_float("chat.timeout", 120.0)
    started_at = int(time.time() * 1000)
    started = time.perf_counter()
    results: list[dict[str, Any]] = []

    for model_id in requested_models:
        spec = model_by_id[model_id]
        item_started = time.perf_counter()
        base = _model_payload(spec)
        try:
            if spec.is_console_chat():
                text = await _probe_console_model(account.token, model_id, timeout_s)
            else:
                text = await _probe_chat_model(account.token, spec, timeout_s)
            results.append(
                {
                    **base,
                    "status": "available",
                    "http_status": 200,
                    "duration_ms": round((time.perf_counter() - item_started) * 1000, 2),
                    "message": "OK",
                    "response_preview": _preview_text(text),
                }
            )
        except Exception as exc:
            error = _error_payload(exc)
            results.append(
                {
                    **base,
                    **error,
                    "duration_ms": round((time.perf_counter() - item_started) * 1000, 2),
                    "response_preview": "",
                }
            )

    available = sum(1 for item in results if item.get("status") == "available")
    report = {
        "object": "grok_capability_report",
        "started_at": started_at,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "token": _account_payload(account),
        "summary": {
            "total": len(results),
            "available": available,
            "failed": len(results) - available,
        },
        "results": results,
    }
    logger.info(
        "admin grok capability scan completed: token={} models={} available={} duration_ms={}",
        _mask_token(account.token),
        len(results),
        available,
        report["duration_ms"],
    )
    return Response(content=orjson.dumps(report), media_type="application/json")


__all__ = ["router"]
