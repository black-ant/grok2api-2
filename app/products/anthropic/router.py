"""Anthropic Messages API router (/v1/messages)."""

from typing import Any

import orjson
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.control.account.quota_defaults import supports_mode
from app.control.account.state_machine import is_manageable
from app.control.model import aliases as model_aliases
from app.control.model.spec import ModelSpec
from app.platform.auth.middleware import verify_api_key
from app.platform.errors import AppError, ValidationError


router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])
_TAG_MESSAGES = "Anthropic - Messages"

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
_POOL_ID_TO_NAME = {0: "basic", 1: "super", 2: "heavy"}


async def _available_pools(request: Request) -> frozenset[str]:
    repo = getattr(request.app.state, "repository", None)
    if repo is None:
        return frozenset()
    snapshot = await repo.runtime_snapshot()
    return frozenset(record.pool for record in snapshot.items if is_manageable(record))


def _model_available_for_pools(spec: ModelSpec, pools: frozenset[str]) -> bool:
    if not spec.enabled:
        return False
    for pool_id in spec.pool_candidates():
        pool = _POOL_ID_TO_NAME[pool_id]
        if pool in pools and supports_mode(pool, int(spec.mode_id)):
            return True
    return False


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class _ContentBlock(BaseModel):
    model_config = {"extra": "allow"}
    type: str = "text"


class _Message(BaseModel):
    model_config = {"extra": "allow"}
    role:    str
    content: Any = ""


class MessagesRequest(BaseModel):
    model_config = {"extra": "ignore"}

    model:       str
    messages:    list[_Message]
    system:      Any = None          # string or array of content blocks
    max_tokens:  int | None = None   # ignored (Grok doesn't expose this param)
    stream:      bool | None = None
    temperature: float | None = None
    top_p:       float | None = None
    tools:       list[dict] | None = None
    tool_choice: Any = None
    thinking:    Any = None          # {type:"enabled", budget_tokens:N} — used to enable thinking output


# ---------------------------------------------------------------------------
# SSE error wrapper
# ---------------------------------------------------------------------------

async def _safe_sse_anthropic(stream):
    """Wrap an Anthropic SSE stream, converting exceptions to error events."""
    try:
        async for chunk in stream:
            yield chunk
    except AppError as exc:
        err = exc.to_dict()["error"]
        payload = orjson.dumps({"type": "error", "error": err}).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        payload = orjson.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": str(exc)},
        }).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# /v1/messages
# ---------------------------------------------------------------------------

@router.post("/messages", tags=[_TAG_MESSAGES])
async def messages_endpoint(req: MessagesRequest, request: Request):
    from app.platform.config.snapshot import get_config

    pools = await _available_pools(request)
    resolved = model_aliases.resolve(
        req.model,
        available_pools=pools,
        is_available=_model_available_for_pools,
    )
    if resolved is None:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model", code="model_not_found",
        )
    request.state.request_log_routing = {
        "model": req.model,
        "resolved_model": resolved.model,
    }
    if resolved.is_virtual:
        request.state.request_log_routing["virtual_model"] = req.model

    if not req.messages:
        raise ValidationError("messages cannot be empty", param="messages")

    cfg       = get_config()
    is_stream = req.stream if req.stream is not None else cfg.get_bool("features.stream", True)

    # thinking flag: enable when request has thinking config or config default
    if req.thinking is not None and isinstance(req.thinking, dict):
        emit_think = req.thinking.get("type") != "disabled"
    else:
        emit_think = cfg.get_bool("features.thinking", True)

    # Convert Pydantic models → plain dicts
    messages = [m.model_dump() for m in req.messages]

    from .messages import create as messages_create
    result = await messages_create(
        model        = resolved.model,
        messages     = messages,
        system       = req.system,
        stream       = is_stream,
        emit_think   = emit_think,
        temperature  = req.temperature or 0.8,
        top_p        = req.top_p or 0.95,
        tools        = req.tools or None,
        tool_choice  = req.tool_choice,
    )

    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_sse_anthropic(result),
        media_type = "text/event-stream",
        headers    = _SSE_HEADERS,
    )


__all__ = ["router"]
