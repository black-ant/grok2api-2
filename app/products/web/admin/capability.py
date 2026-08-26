"""Admin Grok account capability detection endpoints."""

from __future__ import annotations

import asyncio
import time
from hashlib import sha256
from random import choice
from typing import TYPE_CHECKING, Any, Literal

import orjson
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.control.account.commands import ListAccountsQuery
from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.account.runtime import get_refresh_service
from app.control.model import registry as model_registry
from app.control.model.enums import ModeId
from app.dataplane.account import get_account_directory
from app.dataplane.account.selector import current_strategy
from app.dataplane.shared.enums import POOL_ID_TO_STR
from app.dataplane.reverse.protocol.xai_chat import StreamAdapter, classify_line
from app.dataplane.reverse.protocol.xai_console_chat import (
    ConsoleStreamAdapter,
    build_console_payload,
    stream_console_chat,
)
from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, ErrorKind, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_s
from app.products._account_selection import reserve_account
from app.products.openai.chat import _stream_chat

from . import get_repo

if TYPE_CHECKING:
    from app.control.account.models import AccountRecord
    from app.control.account.repository import AccountRepository
    from app.control.model.spec import ModelSpec


router = APIRouter(prefix="/grok-capability", tags=["Admin - Grok Capability"])

_PAGE_SIZE = 2000
_SCAN_QUESTIONS: tuple[str, ...] = (
    "请用一句话说明雨后为什么会出现彩虹。",
    "请用一句话回答：2 加 3 等于几？",
    "请用一句话说明水为什么会结冰。",
    "请用一句话解释什么是 API。",
    "请用一句话说出太阳从哪个方向升起。",
    "请用一句话说明猫为什么会打呼噜。",
    "请用一句话解释什么是开源软件。",
    "请用一句话回答：一年通常有多少个月？",
    "请用一句话说明为什么要备份数据。",
    "请用一句话解释什么是模型推理。",
)


class CapabilityScanRequest(BaseModel):
    model: str = Field(min_length=1)
    token_id: str | None = Field(default=None, min_length=16)
    selection_mode: Literal["key", "routing"] = "key"


def _token_id(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def _mask_token(token: str) -> str:
    if len(token) <= 20:
        return token
    return f"{token[:8]}...{token[-8:]}"


def _token_tail(token: str) -> str:
    value = str(token or "").strip()
    return value[-4:] if value else "-"


def _log_task_exception(task: "asyncio.Task") -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("admin capability account sync failed: error={}", task.exception())


async def _sync_routing_outcome(
    token: str,
    mode_id: int,
    *,
    success: bool,
    exc: BaseException | None = None,
) -> None:
    service = get_refresh_service()
    if service is None:
        return
    if success:
        if current_strategy() != "quota" and mode_id != int(ModeId.CONSOLE):
            return
        await service.refresh_call_async(token, mode_id)
        return
    await service.record_failure_async(token, mode_id, exc)
    if (
        current_strategy() == "quota"
        and mode_id != int(ModeId.CONSOLE)
        and getattr(exc, "status", None) == 429
    ):
        await service.refresh_on_demand()


def _schedule_routing_outcome_sync(
    token: str,
    mode_id: int,
    *,
    success: bool,
    exc: BaseException | None = None,
) -> None:
    task = asyncio.create_task(
        _sync_routing_outcome(token, mode_id, success=success, exc=exc)
    )
    task.add_done_callback(_log_task_exception)


def _account_payload(record: "AccountRecord") -> dict[str, Any]:
    email = str((record.ext or {}).get("email") or "").strip()
    token_mask = _mask_token(record.token)
    label = (
        f"{email} · {token_mask} · {record.pool or 'basic'} · {record.status}"
        if email
        else f"{token_mask} · {record.pool or 'basic'} · {record.status}"
    )
    return {
        "id": _token_id(record.token),
        "label": label,
        "email": email,
        "token_mask": token_mask,
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


def _scan_question() -> str:
    return choice(_SCAN_QUESTIONS)


async def _probe_console_model(
    token: str,
    model_id: str,
    timeout_s: float,
    question: str,
) -> str:
    adapter = ConsoleStreamAdapter()
    parts: list[str] = []
    payload = build_console_payload(
        messages=[{"role": "user", "content": question}],
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


async def _probe_chat_model(
    token: str,
    spec: "ModelSpec",
    timeout_s: float,
    question: str,
    *,
    mode_id: int | None = None,
) -> str:
    adapter = StreamAdapter()
    parts: list[str] = []
    try:
        selected_mode_id = ModeId(int(spec.mode_id if mode_id is None else mode_id))
    except ValueError as exc:
        invalid_mode_id = spec.mode_id if mode_id is None else mode_id
        raise UpstreamError(f"Unsupported chat mode {int(invalid_mode_id)}", status=400) from exc

    async for line in _stream_chat(
        token=token,
        mode_id=selected_mode_id,
        message=question,
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
                "selection_strategy": current_strategy(),
                "default_selection_mode": "routing",
            }
        ),
        media_type="application/json",
    )


@router.post("/scan")
async def scan_capabilities(
    req: CapabilityScanRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    model_by_id = {spec.model_name: spec for spec in _scan_models()}
    model_id = req.model.strip()
    spec = model_by_id.get(model_id)
    if spec is None:
        raise ValidationError(
            f"Unsupported scan model: {model_id}",
            param="model",
        )

    directory = None
    lease = None
    selected_token = ""
    selected_account_pool: str | None = None
    selected_mode_id = int(spec.mode_id)
    if req.selection_mode == "routing":
        directory = await get_account_directory(repo)
        lease, selected_mode_id = await reserve_account(directory, spec)
        selected_token = lease.token if lease is not None else ""
        selected_account_pool = (
            POOL_ID_TO_STR.get(lease.pool_id) if lease is not None else None
        )
        account = (
            next(iter(await repo.get_accounts([lease.token])), None)
            if lease is not None
            else None
        )
    else:
        if not req.token_id:
            raise ValidationError("token_id is required for key mode", param="token_id")
        account = await _find_account(repo, req.token_id)
        if account is None:
            raise ValidationError("Grok token not found", param="token_id")
        selected_token = account.token
        selected_account_pool = account.pool or "basic"

    timeout_s = get_config().get_float("chat.timeout", 120.0)
    question = _scan_question()
    started_at = int(time.time() * 1000)
    started = time.perf_counter()
    results: list[dict[str, Any]] = []

    item_started = time.perf_counter()
    base = {
        **_model_payload(spec),
        "selection_mode": req.selection_mode,
        "selection_strategy": current_strategy(),
        "selected_mode_id": selected_mode_id,
        "account_pool": selected_account_pool,
    }
    if selected_token:
        base.update(
            {
                "token_id": _token_id(selected_token),
                "token_tail": _token_tail(selected_token),
            }
        )

    fail_exc: BaseException | None = None
    success = False
    if not selected_token:
        results.append(
            {
                **base,
                "status": "unavailable",
                "http_status": 503,
                "error_type": ErrorKind.SERVER,
                "error_code": "account_unavailable",
                "message": "实际发送池无可用 Key",
                "body_preview": "",
                "duration_ms": round((time.perf_counter() - item_started) * 1000, 2),
                "question": question,
                "response_preview": "",
            }
        )
    else:
        try:
            if spec.is_console_chat():
                text = await _probe_console_model(selected_token, model_id, timeout_s, question)
            else:
                text = await _probe_chat_model(
                    selected_token,
                    spec,
                    timeout_s,
                    question,
                    mode_id=selected_mode_id,
                )
            if not text.strip():
                raise UpstreamError("模型未返回消息", status=502)
            success = True
            results.append(
                {
                    **base,
                    "status": "available",
                    "http_status": 200,
                    "duration_ms": round((time.perf_counter() - item_started) * 1000, 2),
                    "message": "OK",
                    "question": question,
                    "response_preview": _preview_text(text),
                }
            )
        except Exception as exc:
            fail_exc = exc
            error = _error_payload(exc)
            results.append(
                {
                    **base,
                    **error,
                    "duration_ms": round((time.perf_counter() - item_started) * 1000, 2),
                    "question": question,
                    "response_preview": "",
                }
            )
        finally:
            if lease is not None and directory is not None:
                await directory.release(lease)
                feedback_kind = (
                    FeedbackKind.SUCCESS
                    if success
                    else feedback_kind_for_error(fail_exc)
                )
                await directory.feedback(
                    lease.token,
                    feedback_kind,
                    selected_mode_id,
                    now_s_val=now_s(),
                )
                _schedule_routing_outcome_sync(
                    lease.token,
                    selected_mode_id,
                    success=success,
                    exc=fail_exc,
                )

    available = sum(1 for item in results if item.get("status") == "available")
    report = {
        "object": "grok_capability_report",
        "started_at": started_at,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "token": _account_payload(account) if account is not None else None,
        "selection_mode": req.selection_mode,
        "selection_strategy": current_strategy(),
        "question": question,
        "summary": {
            "total": len(results),
            "available": available,
            "failed": len(results) - available,
        },
        "results": results,
    }
    logger.info(
        "admin grok capability scan completed: token={} model={} available={} duration_ms={}",
        _mask_token(selected_token) if selected_token else "-",
        model_id,
        available,
        report["duration_ms"],
    )
    return Response(content=orjson.dumps(report), media_type="application/json")


__all__ = ["router"]
