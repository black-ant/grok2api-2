"""OpenAI-compatible API router (/v1/*)."""

import base64
import binascii
import hashlib
import mimetypes
import time
from typing import Annotated, AsyncGenerator, AsyncIterable, Literal

import orjson
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from app.control.account.state_machine import is_manageable
from app.platform.auth.middleware import verify_api_key
from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.storage import image_files_dir, video_files_dir
from app.control.model import aliases as model_aliases
from app.control.model.cooldown import blocked_models
from app.control.model import registry as model_registry
from app.control.model.enums import ModeId
from app.control.model.spec import ModelSpec
from app.control.account.quota_defaults import supports_mode
from .schemas import (
    ChatCompletionRequest,
    ImageEditRequest,
    ImageGenerationRequest,
    VideoConfig,
    VideoGenerationRequest,
    ImageConfig,
    ResponsesCreateRequest,
)
from .chat import completions as chat_completions

router = APIRouter(prefix="/v1")
_POOL_ID_TO_NAME = {0: "basic", 1: "super", 2: "heavy"}
_TAG_MODELS = "OpenAI - Models"
_TAG_CHAT = "OpenAI - Chat"
_TAG_RESPONSES = "OpenAI - Responses"
_TAG_IMAGES = "OpenAI - Images"
_TAG_VIDEOS = "OpenAI - Videos"
_TAG_AUDIO = "OpenAI - Audio"
_TAG_FILES = "OpenAI - Files"

_CODEX_MODEL_METADATA: dict[str, dict[str, object]] = {
    "grok-4.3": {
        "context_window": 1_000_000,
        "description": "xAI Grok 4.3 high-capacity reasoning model.",
    },
    "grok-4.5": {
        "context_window": 500_000,
        "description": "xAI Grok 4.5 frontier model with reasoning and vision.",
    },
    "grok-4.6": {
        "context_window": 500_000,
        "description": "xAI Grok 4.6 frontier model with reasoning and vision.",
    },
    "grok-4.20-0309": {
        "context_window": 2_000_000,
        "description": "xAI Grok 4.20 reasoning model.",
    },
    "grok-4.20-0309-reasoning": {
        "context_window": 2_000_000,
        "description": "xAI Grok 4.20 reasoning model.",
    },
    "grok-4.20-0309-non-reasoning": {
        "context_window": 2_000_000,
        "description": "xAI Grok 4.20 non-reasoning model.",
    },
    "grok-4.20-multi-agent-0309": {
        "context_window": 2_000_000,
        "description": "xAI Grok 4.20 multi-agent model.",
    },
    "grok-build-0.1": {
        "context_window": 256_000,
        "description": "xAI Grok Build 0.1 coding model.",
    },
    "grok-build-console": {
        "context_window": 256_000,
        "description": "xAI Grok Build 0.1 coding model.",
    },
}


async def _available_pools(request: Request) -> frozenset[str]:
    repo = getattr(request.app.state, "repository", None)
    if repo is None:
        return frozenset()

    snapshot = await repo.runtime_snapshot()
    pools = {record.pool for record in snapshot.items if is_manageable(record)}
    return frozenset(pools)


def _model_available_for_pools(spec: ModelSpec, pools: frozenset[str]) -> bool:
    if not spec.enabled:
        return False
    for pool_id in spec.pool_candidates():
        pool = _POOL_ID_TO_NAME[pool_id]
        if pool in pools and supports_mode(pool, int(spec.mode_id)):
            return True
    return False


async def _resolve_model_for_request(
    model_name: str,
    request: Request | None = None,
    *,
    require_api: bool = True,
):
    pools = await _available_pools(request) if request is not None else None
    resolved = model_aliases.resolve(
        model_name,
        available_pools=pools,
        is_available=_model_available_for_pools,
        blocked_model_names=blocked_models(),
    )
    if resolved is None:
        return None
    if resolved.is_virtual and not model_aliases.is_resolution_usable(resolved):
        logger.error(
            "virtual model resolved to incompatible candidate: alias={} candidate={}",
            model_name,
            resolved.model,
        )
        return None
    if (
        pools is not None
        and not resolved.is_virtual
        and not _model_available_for_pools(resolved.spec, pools)
    ):
        return None
    if require_api and not resolved.spec.supported_in_api:
        return None
    return resolved


def _set_request_log_state(request: Request, **values: object) -> None:
    state = getattr(request, "state", None)
    if state is None:
        return
    for key, value in values.items():
        setattr(state, key, value)


def _message_image_count(messages: list[dict]) -> int:
    return sum(
        1
        for message in messages
        for item in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(item, dict) and item.get("type") in {"image_url", "input_image"}
    )


def _standard_model_entry(
    *,
    model_id: str,
    public_name: str,
    created: int,
    supported_in_api: bool = True,
    availability_note: str | None = None,
    virtual: bool = False,
    resolved_model: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": "xai",
        "name": public_name,
        "supported_in_api": bool(supported_in_api),
    }
    if not supported_in_api:
        if availability_note:
            entry["availability_note"] = availability_note
    if virtual:
        entry["virtual"] = True
        if resolved_model:
            entry["resolved_model"] = resolved_model
    return entry


def _codex_reasoning_levels(model_id: str, spec: ModelSpec) -> list[str]:
    if not (spec.is_chat() or spec.is_console_chat()):
        return []
    for suffix in ("xhigh", "high", "medium", "low"):
        if model_id.endswith(f"-{suffix}"):
            return [suffix]
    if "reasoning" in model_id or model_id.endswith(("-expert", "-heavy")):
        return ["high"]
    if int(spec.mode_id) == 1:
        return ["none"]
    if model_id in {"grok-4.5", "grok-4.5-console"}:
        return ["low", "medium", "high"]
    if model_id == "grok-4.6":
        return ["none", "low", "medium", "high", "xhigh"]
    if model_id in {"grok-build-0.1", "grok-build-console"}:
        return ["none"]
    return ["none", "low", "medium", "high"]


def _codex_model_catalog(
    entries: list[tuple[str, ModelSpec, str]],
) -> dict[str, list[dict[str, object]]]:
    descriptions = {
        "none": "No reasoning",
        "low": "Fast responses with lighter reasoning",
        "medium": "Balances speed and reasoning depth for everyday tasks",
        "high": "Greater reasoning depth for complex problems",
        "xhigh": "Extra high reasoning depth for complex problems",
    }
    models: list[dict[str, object]] = []
    for index, (model_id, spec, public_name) in enumerate(entries, start=1):
        metadata = _CODEX_MODEL_METADATA.get(model_id, {})
        context_window = int(metadata.get("context_window", 128_000))
        levels = _codex_reasoning_levels(model_id, spec)
        default_level = levels[0] if levels else "none"
        if levels == ["high"]:
            default_level = "high"
        if levels == ["xhigh"]:
            default_level = "xhigh"
        reasoning_levels = [
            {"effort": level, "description": descriptions[level]}
            for level in levels
        ]
        is_media = spec.is_image() or spec.is_image_edit() or spec.is_video()
        model_entry: dict[str, object] = {
            "slug": model_id,
            "display_name": public_name,
            "description": str(metadata.get("description", public_name)),
            "default_reasoning_level": default_level,
            "supported_reasoning_levels": reasoning_levels,
            "shell_type": "shell_command",
            "visibility": "hide" if is_media else "list",
            "minimal_client_version": "0.0.0",
            "supported_in_api": True,
            "priority": index,
            "additional_speed_tiers": [],
            "service_tiers": [],
            "default_service_tier": None,
            "availability_nux": None,
            "upgrade": None,
            "base_instructions": "",
            "model_messages": None,
            "include_skills_usage_instructions": False,
            "supports_reasoning_summary_parameter": bool(levels),
            "supports_reasoning_summaries": bool(levels),
            "default_reasoning_summary": "auto",
            "support_verbosity": False,
            "default_verbosity": None,
            "apply_patch_tool_type": None,
            "web_search_tool_type": "",
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "supports_parallel_tool_calls": bool(
                spec.is_chat() or spec.is_console_chat()
            ),
            "supports_image_detail_original": False,
            "context_window": context_window,
            "max_context_window": context_window,
            "auto_compact_token_limit": None,
            "effective_context_window_percent": 95,
            "experimental_supported_tools": [],
            "input_modalities": ["text", "image"]
            if (spec.is_chat() or spec.is_console_chat())
            else ["text"],
            "supports_search_tool": False,
            "use_responses_lite": False,
        }
        model_entry["supported_in_api"] = bool(spec.supported_in_api)
        if not spec.supported_in_api and spec.availability_note:
            model_entry["availability_note"] = spec.availability_note
        models.append(model_entry)
    return {"models": models}


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


@router.get("/models", tags=[_TAG_MODELS], dependencies=[Depends(verify_api_key)])
async def list_models(
    request: Request,
    client_version: str | None = Query(default=None),
):
    pools = await _available_pools(request)
    created = int(time.time())
    virtual_models = [
        (
            resolved.requested_model,
            resolved.spec,
            resolved.requested_model,
        )
        for resolved in model_aliases.list_virtual_models(
            available_pools=pools,
            is_available=_model_available_for_pools,
        )
    ]
    models = [
        (spec.model_name, spec, spec.public_name)
        for spec in model_registry.list_enabled()
        if _model_available_for_pools(spec, pools)
    ]
    entries: list[tuple[str, ModelSpec, str]] = []
    seen: set[str] = set()
    for entry in (*virtual_models, *models):
        if entry[0] in seen:
            continue
        seen.add(entry[0])
        entries.append(entry)

    if isinstance(client_version, str) and client_version.strip():
        body = orjson.dumps(_codex_model_catalog(entries))
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        headers = {"ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content=body, media_type="application/json", headers=headers)

    data = []
    for model_id, spec, public_name in entries:
        virtual = model_id != spec.model_name
        supported_in_api = (
            model_aliases.alias_supported_in_api(model_id)
            if virtual
            else spec.supported_in_api
        )
        data.append(
            _standard_model_entry(
                model_id=model_id,
                public_name=public_name,
                created=created,
                supported_in_api=bool(supported_in_api),
                availability_note=spec.availability_note,
                virtual=virtual,
                resolved_model=spec.model_name if virtual else None,
            )
        )
    return JSONResponse({"object": "list", "data": data})


@router.get(
    "/models/{model_id}", tags=[_TAG_MODELS], dependencies=[Depends(verify_api_key)]
)
async def get_model_endpoint(model_id: str, request: Request):
    resolved = await _resolve_model_for_request(
        model_id, request, require_api=False
    )
    if resolved is None:
        return JSONResponse(
            {
                "error": {
                    "message": f"Model {model_id!r} not found",
                    "type": "invalid_request_error",
                }
            },
            status_code=404,
        )
    supported_in_api = (
        model_aliases.alias_supported_in_api(resolved.requested_model)
        if resolved.is_virtual
        else resolved.spec.supported_in_api
    )
    payload = {
            "id": resolved.requested_model,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "xai",
            "name": resolved.requested_model if resolved.is_virtual else resolved.spec.public_name,
            "supported_in_api": bool(supported_in_api),
        }
    if resolved.is_virtual:
        payload["virtual"] = True
        payload["resolved_model"] = resolved.model
    if not supported_in_api and resolved.spec.availability_note:
        payload["availability_note"] = resolved.spec.availability_note
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# SSE streaming helpers
# ---------------------------------------------------------------------------


async def _safe_sse(stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
    """Wrap an SSE stream, converting exceptions to in-band error events."""
    try:
        async for chunk in stream:
            yield chunk
    except AppError as exc:
        payload = orjson.dumps({"error": exc.to_dict()["error"]}).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        payload = orjson.dumps(
            {"error": {"message": str(exc), "type": "server_error"}}
        ).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


async def _chain_first_sse(
    first: str, stream: AsyncIterable[str]
) -> AsyncGenerator[str, None]:
    yield first
    async for chunk in stream:
        yield chunk


async def _sse_response_or_error(stream: AsyncIterable[str]):
    """Return an SSE response, or an HTTP error before headers are sent."""
    iterator = stream.__aiter__()
    try:
        first = await anext(iterator)
    except StopAsyncIteration:
        return StreamingResponse(
            iter(()), media_type="text/event-stream", headers=_SSE_HEADERS
        )
    except AppError as exc:
        return JSONResponse(exc.to_dict(), status_code=exc.status)
    except Exception as exc:
        logger.exception("chat completions stream failed before first chunk: error={}", exc)
        payload = {"error": {"message": str(exc), "type": "server_error"}}
        return JSONResponse(payload, status_code=500)
    return StreamingResponse(
        _safe_sse(_chain_first_sse(first, iterator)),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}


# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------

_VALID_ROLES = {"developer", "system", "user", "assistant", "tool"}
_USER_BLOCK_TYPES = {"text", "image_url", "input_audio", "file"}
_ALLOWED_SIZES = {"1280x720", "720x1280", "1792x1024", "1024x1792", "1024x1024"}
_ASPECT_RATIO_TO_SIZE = {
    "16:9": "1280x720",
    "9:16": "720x1280",
    "3:2": "1792x1024",
    "2:3": "1024x1792",
    "1:1": "1024x1024",
}
_VIDEO_ASPECT_RATIO_TO_SIZE = {
    "16:9": "1280x720",
    "9:16": "720x1280",
    "1:1": "1024x1024",
}
_EFFORT_VALUES = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
_LITE_IMAGE_MODELS = {"grok-imagine-image-lite"}


def _validate_chat(req: ChatCompletionRequest) -> None:
    from app.platform.errors import ValidationError

    if not req.messages:
        raise ValidationError("messages cannot be empty", param="messages")
    for i, msg in enumerate(req.messages):
        if msg.role not in _VALID_ROLES:
            raise ValidationError(
                f"role must be one of {sorted(_VALID_ROLES)}",
                param=f"messages.{i}.role",
            )
    if req.temperature is not None and not (0 <= req.temperature <= 2):
        raise ValidationError(
            "temperature must be between 0 and 2", param="temperature"
        )
    if req.top_p is not None and not (0 <= req.top_p <= 1):
        raise ValidationError("top_p must be between 0 and 1", param="top_p")
    if req.reasoning_effort is not None and req.reasoning_effort not in _EFFORT_VALUES:
        raise ValidationError(
            f"reasoning_effort must be one of {sorted(_EFFORT_VALUES)}",
            param="reasoning_effort",
        )


def _validate_image_n(model_name: str, n: int, *, param: str) -> None:
    max_n = 4 if model_name in _LITE_IMAGE_MODELS else 10
    if not (1 <= n <= max_n):
        raise ValidationError(
            f"n must be between 1 and {max_n} for model {model_name!r}",
            param=param,
        )


def _validate_image_edit_n(n: int, *, param: str, model: str | None = None) -> None:
    maximum = 10 if model in {
        "grok-imagine-image-quality",
        "grok-imagine-image-2.0",
        "grok-imagine-image-quality-2.0",
    } else 2
    if not (1 <= n <= maximum):
        raise ValidationError(
            f"n must be between 1 and {maximum} for image edit",
            param=param,
        )


def _resolve_image_size(size: str | None, aspect_ratio: str | None) -> str:
    if aspect_ratio:
        normalized = aspect_ratio.strip().lower()
        mapped = _ASPECT_RATIO_TO_SIZE.get(normalized)
        if mapped is None:
            raise ValidationError(
                "aspect_ratio must be one of [1:1, 2:3, 3:2, 9:16, 16:9]",
                param="aspect_ratio",
            )
        return mapped
    return (size or "1024x1024").strip()


async def _upload_to_data_uri(upload: UploadFile, *, param: str) -> str:
    max_bytes = max(1, get_config().get_int("asset.input_max_mb", 20)) * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk_size = min(1024 * 1024, max_bytes - total + 1)
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValidationError(
                f"Uploaded file exceeds the {max_bytes // (1024 * 1024)} MB limit",
                param=param,
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise ValidationError("Uploaded image cannot be empty", param=param)

    mime = (
        (upload.content_type or "").strip().lower()
        or mimetypes.guess_type(upload.filename or "")[0]
        or "application/octet-stream"
    )
    if not mime.startswith("image/"):
        raise ValidationError("Uploaded file must be an image", param=param)

    try:
        blob_b64 = base64.b64encode(raw).decode("ascii")
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValidationError("Failed to encode uploaded image", param=param) from exc
    return f"data:{mime};base64,{blob_b64}"


def _normalize_json_image_reference(value: object, *, param: str) -> str:
    if isinstance(value, dict):
        file_id = str(value.get("file_id") or "").strip()
        if file_id:
            raise ValidationError(
                "image.file_id is not supported yet", param=f"{param}.file_id"
            )
        value = value.get("url")
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("image.url is required", param=param)
    return value.strip()


def _parse_form_int(value: object, *, default: int, param: str) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError("must be an integer", param=param) from exc


@router.post(
    "/chat/completions", tags=[_TAG_CHAT], dependencies=[Depends(verify_api_key)]
)
async def chat_completions_endpoint(req: ChatCompletionRequest, request: Request):
    _validate_chat(req)
    resolved = await _resolve_model_for_request(req.model, request)
    if resolved is None:
        if model_aliases.is_virtual_model(req.model):
            raise UpstreamError(
                f"Virtual model {req.model!r} has no compatible route.",
                status=503,
            )
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    real_model = resolved.model
    spec = resolved.spec
    request.state.request_log_routing = {
        "model": req.model,
        "resolved_model": real_model,
    }
    if resolved.is_virtual:
        request.state.request_log_routing["virtual_model"] = req.model
        request.state.request_log_routing["model_pool"] = resolved.pool
        request.state.request_log_routing["fallback_candidate_pools"] = (
            model_aliases.fallback_candidate_pools(resolved)
        )
    request.state.request_log_routing["route"] = (
        "console" if spec.is_console_chat() else "grok"
    )
    from app.platform.config.snapshot import get_config

    cfg = get_config()
    is_stream = (
        req.stream if req.stream is not None else cfg.get_bool("features.stream", True)
    )
    operation = (
        "image_edit" if spec.is_image_edit()
        else "image" if spec.is_image()
        else "video" if spec.is_video()
        else "chat"
    )
    request.state.request_log_operation = operation
    request.state.request_log_streaming = is_stream
    media_usage: dict[str, int] = {}
    if spec.is_image_edit():
        media_usage["media_input_images"] = _message_image_count(
            [m.model_dump(exclude_none=True) for m in req.messages]
        )
        media_usage["media_output_images"] = (req.image_config or ImageConfig()).n or 1
    elif spec.is_image():
        media_usage["media_output_images"] = (req.image_config or ImageConfig()).n or 1
    elif spec.is_video():
        media_usage["media_output_seconds"] = (req.video_config or VideoConfig()).seconds or 6
    if media_usage:
        request.state.request_log_usage = media_usage

    messages = [m.model_dump(exclude_none=True) for m in req.messages]

    try:
        # Dispatch by model capability.
        if spec.is_image_edit():
            from .images import edit as img_edit

            cfg = req.image_config or ImageConfig()
            _validate_image_edit_n(cfg.n or 1, param="image_config.n", model=real_model)
            result = await img_edit(
                model=real_model,
                messages=messages,
                n=cfg.n or 1,
                size=cfg.size or "1024x1024",
                aspect_ratio=cfg.aspect_ratio,
                resolution=cfg.resolution,
                quality=cfg.quality,
                response_format=cfg.response_format or "url",
                stream=is_stream,
                chat_format=True,
            )

        elif spec.is_image():
            from .images import generate as img_gen

            cfg = req.image_config or ImageConfig()
            size = cfg.size or "1024x1024"
            fmt = cfg.response_format or "url"
            n = cfg.n or 1
            _validate_image_n(real_model, n, param="image_config.n")
            # Extract prompt from last user message.
            prompt = next(
                (
                    m.content
                    for m in reversed(req.messages)
                    if m.role == "user"
                    and isinstance(m.content, str)
                    and m.content.strip()
                ),
                "",
            )
            result = await img_gen(
                model=real_model,
                prompt=prompt or "",
                n=n,
                size=size,
                aspect_ratio=cfg.aspect_ratio,
                resolution=cfg.resolution,
                quality=cfg.quality,
                response_format=fmt,
                stream=is_stream,
                chat_format=True,
            )

        elif spec.is_video():
            from .video import completions as vid_comp

            vcfg = req.video_config or VideoConfig()
            from .video import validate_video_length as _validate_video_length

            if spec.mode_id != ModeId.CONSOLE:
                _validate_video_length(vcfg.seconds or 6)
            result = await vid_comp(
                model=real_model,
                messages=messages,
                stream=is_stream,
                seconds=vcfg.seconds or 6,
                size=vcfg.size or "720x1280",
                resolution_name=vcfg.resolution_name,
                preset=vcfg.preset,
            )

        else:
            # reasoning_effort=None → config default; "none" → off; otherwise → on.
            if req.reasoning_effort is None:
                emit_think: bool | None = None
            else:
                emit_think = req.reasoning_effort != "none"
            result = await chat_completions(
                model=real_model,
                messages=messages,
                stream=is_stream,
                emit_think=emit_think,
                reasoning_effort=req.reasoning_effort,
                tools=req.tools,
                tool_choice=req.tool_choice,
                temperature=req.temperature or 0.8,
                top_p=req.top_p or 0.95,
                model_fallbacks=model_aliases.fallback_candidates(resolved),
                request_log_routing=request.state.request_log_routing,
            )

    except AppError:
        raise
    except Exception as exc:
        logger.exception(
            "chat completions endpoint failed: model={} stream={} error={}",
            real_model,
            is_stream,
            exc,
        )
        if is_stream:
            _err_msg = str(
                exc
            )  # capture before Python clears the except-scope variable

            async def _err_stream():
                payload = orjson.dumps(
                    {"error": {"message": _err_msg, "type": "server_error"}}
                ).decode()
                yield f"event: error\ndata: {payload}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _err_stream(), media_type="text/event-stream", headers=_SSE_HEADERS
            )
        raise

    if isinstance(result, dict):
        return JSONResponse(result)
    return await _sse_response_or_error(result)


# ---------------------------------------------------------------------------
# /v1/responses  (OpenAI Responses API)
# ---------------------------------------------------------------------------


async def _safe_sse_responses(stream) -> AsyncGenerator[str, None]:
    """SSE wrapper that converts errors to Responses API error events."""
    try:
        async for chunk in stream:
            yield chunk
    except Exception as exc:
        from app.platform.errors import AppError

        if isinstance(exc, AppError):
            err = exc.to_dict()["error"]
        else:
            err = {
                "message": str(exc),
                "type": "server_error",
                "code": None,
                "param": None,
            }
        payload = orjson.dumps({"type": "error", **err}).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


@router.post(
    "/responses", tags=[_TAG_RESPONSES], dependencies=[Depends(verify_api_key)]
)
async def responses_endpoint(req: ResponsesCreateRequest, request: Request):
    from app.platform.config.snapshot import get_config
    from app.platform.errors import ValidationError as _ValidationError

    resolved = await _resolve_model_for_request(req.model, request)
    if resolved is None:
        raise _ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    request.state.request_log_routing = {
        "model": req.model,
        "resolved_model": resolved.model,
    }
    if resolved.is_virtual:
        request.state.request_log_routing["virtual_model"] = req.model
        request.state.request_log_routing["model_pool"] = resolved.pool
    request.state.request_log_operation = "responses"
    if not req.input:
        raise _ValidationError("input cannot be empty", param="input")

    cfg = get_config()
    is_stream = (
        req.stream if req.stream is not None else cfg.get_bool("features.stream", True)
    )
    request.state.request_log_streaming = is_stream

    # Map reasoning param → emit_think flag.
    # reasoning=None → use config; reasoning.effort="none" → off; otherwise on.
    reasoning_effort = None
    if req.reasoning is None:
        emit_think = cfg.get_bool("features.thinking", True)
    else:
        reasoning_effort = req.reasoning.get("effort") if isinstance(req.reasoning, dict) else None
        emit_think = reasoning_effort != "none"

    from .responses import create as responses_create

    result = await responses_create(
        model=resolved.model,
        input_val=req.input,
        instructions=req.instructions,
        stream=is_stream,
        emit_think=emit_think,
        reasoning_effort=reasoning_effort,
        temperature=req.temperature or 0.8,
        top_p=req.top_p or 0.95,
        tools=req.tools or None,
        tool_choice=req.tool_choice,
    )

    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_sse_responses(result),
        media_type = "text/event-stream",
        headers    = _SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# /v1/images/generations (standalone image endpoint)
# ---------------------------------------------------------------------------


@router.post(
    "/images/generations", tags=[_TAG_IMAGES], dependencies=[Depends(verify_api_key)]
)
async def image_generations(req: ImageGenerationRequest, request: Request):
    resolved = await _resolve_model_for_request(req.model, request)
    if resolved is None or not resolved.spec.is_image():
        raise ValidationError(
            f"Model {req.model!r} is not an image model", param="model"
        )
    _set_request_log_state(
        request,
        request_log_routing={"model": req.model, "resolved_model": resolved.model},
        request_log_operation="image",
        request_log_streaming=False,
        request_log_usage={"media_output_images": req.n or 1},
    )
    if req.stream:
        raise ValidationError(
            "streaming image generation is not supported by this provider",
            param="stream",
        )
    _validate_image_n(resolved.model, req.n or 1, param="n")

    from .images import generate as img_gen

    result = await img_gen(
        model=resolved.model,
        prompt=req.prompt,
        n=req.n or 1,
        size=_resolve_image_size(req.size, req.aspect_ratio),
        aspect_ratio=req.aspect_ratio,
        resolution=req.resolution,
        quality=req.quality,
        response_format=req.response_format or "url",
        stream=False,
        chat_format=False,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# /v1/videos (OpenAI videos.create surface)
# ---------------------------------------------------------------------------


@router.post("/videos", tags=[_TAG_VIDEOS], dependencies=[Depends(verify_api_key)])
async def videos_create(
    request: Request,
    model: Annotated[str, Form(...)],
    prompt: Annotated[str, Form(...)],
    seconds: Annotated[int, Form()] = 6,
    size: Annotated[
        Literal["720x1280", "1280x720", "1024x1024", "1024x1792", "1792x1024"], Form()
    ] = "720x1280",
    resolution_name: Annotated[Literal["480p", "720p"] | None, Form()] = None,
    preset: Annotated[
        Literal["fun", "normal", "spicy", "custom"] | None, Form()
    ] = None,
    input_reference: Annotated[
        list[UploadFile] | None, File(alias="input_reference[]")
    ] = None,
    idempotency_key: Annotated[str | None, Form()] = None,
):
    from .video import create_video

    requested_model = model or "grok-video"
    resolved = await _resolve_model_for_request(requested_model, request)
    if resolved is None or not resolved.spec.is_video():
        raise ValidationError(
            f"Model {requested_model!r} is not a video model", param="model"
        )
    _set_request_log_state(
        request,
        request_log_routing={"model": requested_model, "resolved_model": resolved.model},
        request_log_operation="video",
        request_log_streaming=False,
        request_log_usage={"media_output_seconds": max(0, seconds)},
    )

    references_payload = None
    if input_reference:
        references_payload = [
            {"image_url": await _upload_to_data_uri(f, param="input_reference")}
            for f in input_reference[:7]
        ]

    result = await create_video(
        model=resolved.model,
        prompt=prompt,
        seconds=seconds,
        size=size or "720x1280",
        resolution_name=resolution_name,
        preset=preset,
        input_references=references_payload,
        idempotency_key=idempotency_key or request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(result)


def _video_json_references(req: VideoGenerationRequest) -> list[dict[str, str]] | None:
    if req.reference_audios:
        raise ValidationError(
            "reference_audios are not supported by this provider",
            param="reference_audios",
        )
    if req.video is not None:
        raise ValidationError(
            "video input is not supported by this provider",
            param="video",
        )

    references = []
    if req.image is not None:
        references.append(req.image)
    references.extend(req.reference_images or [])
    if len(references) > 7:
        raise ValidationError(
            "at most 7 reference_images are supported", param="reference_images"
        )
    if not references:
        return None

    result: list[dict[str, str]] = []
    for index, reference in enumerate(references):
        if reference.file_id:
            raise ValidationError(
                "reference image file_id is not supported yet",
                param=f"reference_images.{index}.file_id",
            )
        if not reference.url or not reference.url.strip():
            raise ValidationError(
                "reference image url is required",
                param=f"reference_images.{index}.url",
            )
        result.append({"image_url": reference.url.strip()})
    return result


@router.post(
    "/videos/generations",
    tags=[_TAG_VIDEOS],
    dependencies=[Depends(verify_api_key)],
)
async def videos_generations(
    req: VideoGenerationRequest,
    request: Request,
):
    from .video import create_video

    resolved = await _resolve_model_for_request(req.model, request)
    if resolved is None or not resolved.spec.is_video():
        raise ValidationError(
            f"Model {req.model!r} is not a video model", param="model"
        )
    _set_request_log_state(
        request,
        request_log_routing={"model": req.model, "resolved_model": resolved.model},
        request_log_operation="video",
        request_log_streaming=False,
        request_log_usage={"media_output_seconds": max(0, req.duration or 6)},
    )

    size = "720x1280"
    if req.aspect_ratio:
        size = _VIDEO_ASPECT_RATIO_TO_SIZE.get(req.aspect_ratio.strip().lower(), "")
        if not size:
            raise ValidationError(
                "aspect_ratio must be one of [1:1, 9:16, 16:9]",
                param="aspect_ratio",
            )

    result = await create_video(
        model=resolved.model,
        prompt=req.prompt,
        seconds=req.duration,
        size=size,
        resolution_name=req.resolution,
        input_references=_video_json_references(req),
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return JSONResponse(result)


@router.get(
    "/videos/{video_id}", tags=[_TAG_VIDEOS], dependencies=[Depends(verify_api_key)]
)
async def videos_retrieve(video_id: str):
    from .video import retrieve

    return JSONResponse(await retrieve(video_id))


@router.get(
    "/videos/{video_id}/content",
    tags=[_TAG_VIDEOS],
    dependencies=[Depends(verify_api_key)],
)
async def videos_content(video_id: str):
    from .video import _safe_video_filename, content_path

    path = await content_path(video_id)

    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    safe_name = _safe_video_filename(video_id, media_type)
    return FileResponse(path, media_type=media_type, filename=safe_name)


# ---------------------------------------------------------------------------
# /v1/audio/* and Console voice WebSockets
# ---------------------------------------------------------------------------


@router.post("/tts", tags=[_TAG_AUDIO], dependencies=[Depends(verify_api_key)])
async def text_to_speech(request: Request):
    from .audio import speech

    return await speech(request)


@router.get("/tts/voices", tags=[_TAG_AUDIO], dependencies=[Depends(verify_api_key)])
async def tts_voices(request: Request):
    from .audio import voices

    return await voices(request)


@router.get(
    "/tts/voices/{voice_id}", tags=[_TAG_AUDIO], dependencies=[Depends(verify_api_key)]
)
async def tts_voice(request: Request, voice_id: str):
    from .audio import voices

    return await voices(request, voice_id=voice_id)


@router.post(
    "/audio/speech", tags=[_TAG_AUDIO], dependencies=[Depends(verify_api_key)]
)
async def audio_speech(request: Request):
    from .audio import speech

    return await speech(request)


@router.post(
    "/audio/tasks", tags=[_TAG_AUDIO], dependencies=[Depends(verify_api_key)]
)
async def audio_task(request: Request):
    from .audio import speech

    return await speech(request)


@router.post(
    "/audio/transcriptions", tags=[_TAG_AUDIO], dependencies=[Depends(verify_api_key)]
)
async def audio_transcriptions(request: Request):
    from .audio import transcriptions

    return await transcriptions(request)


@router.post("/stt", tags=[_TAG_AUDIO], dependencies=[Depends(verify_api_key)])
async def speech_to_text(request: Request):
    from .audio import transcriptions

    return await transcriptions(request)


@router.websocket("/realtime")
async def realtime_websocket(websocket: WebSocket):
    from .audio import websocket_proxy

    await websocket_proxy(websocket, path="/realtime")


@router.websocket("/stt")
async def stt_websocket(websocket: WebSocket):
    from .audio import websocket_proxy

    await websocket_proxy(websocket, path="/stt")


# ---------------------------------------------------------------------------
# /v1/images/edits (standalone image-edit endpoint)
# ---------------------------------------------------------------------------


@router.post(
    "/images/edits", tags=[_TAG_IMAGES], dependencies=[Depends(verify_api_key)]
)
async def image_edits(
    request: Request,
):
    content_type = request.headers.get("content-type", "").lower()
    image_inputs: list[str] = []
    mask: object = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    quality: str | None = None
    stream = False

    if content_type.startswith("multipart/"):
        form = await request.form()
        model = str(form.get("model") or "").strip()
        prompt = str(form.get("prompt") or "")
        raw_images = form.getlist("image[]") or form.getlist("image")
        uploads = [item for item in raw_images if hasattr(item, "read")]
        if not uploads:
            raise ValidationError("image is required", param="image")
        mask = form.get("mask")
        n = _parse_form_int(form.get("n"), default=1, param="n")
        size = str(form.get("size") or "1024x1024")
        response_format = str(form.get("response_format") or "url")
        aspect_ratio = str(form.get("aspect_ratio") or "") or None
        resolution = str(form.get("resolution") or "") or None
        quality = str(form.get("quality") or "") or None
        image_inputs = [
            await _upload_to_data_uri(item, param=f"image.{index}")
            for index, item in enumerate(uploads)
        ]
    elif content_type.startswith("application/json"):
        try:
            payload = ImageEditRequest.model_validate(await request.json())
        except Exception as exc:
            raise ValidationError("Invalid image edit request", param="body") from exc
        model = payload.model
        prompt = payload.prompt
        raw_images = payload.image if isinstance(payload.image, list) else [payload.image]
        image_inputs = [
            _normalize_json_image_reference(item, param=f"image.{index}")
            for index, item in enumerate(raw_images)
        ]
        mask = payload.mask
        n = payload.n or 1
        size = payload.size or "1024x1024"
        response_format = payload.response_format or "url"
        aspect_ratio = payload.aspect_ratio
        resolution = payload.resolution
        quality = payload.quality
        stream = bool(payload.stream)
    else:
        raise ValidationError(
            "images/edits requires multipart/form-data or application/json",
            param="Content-Type",
        )

    resolved = await _resolve_model_for_request(model, request)
    if resolved is None or not resolved.spec.is_image_edit():
        raise ValidationError(
            f"Model {model!r} is not an image-edit model", param="model"
        )
    _set_request_log_state(
        request,
        request_log_routing={"model": model, "resolved_model": resolved.model},
        request_log_operation="image_edit",
        request_log_streaming=False,
        request_log_usage={
            "media_input_images": len(image_inputs),
            "media_output_images": n,
        },
    )

    if mask is not None:
        raise ValidationError("mask is not supported yet", param="mask")
    if stream:
        raise ValidationError(
            "streaming image editing is not supported by this provider",
            param="stream",
        )
    if (
        aspect_ratio
        and resolved.spec.mode_id != ModeId.CONSOLE
        and aspect_ratio.strip() != "1:1"
    ):
        raise ValidationError(
            "image editing currently supports only aspect_ratio 1:1",
            param="aspect_ratio",
        )
    _validate_image_edit_n(n, param="n", model=resolved.model)

    from .images import edit as img_edit

    # Wrap input into a single-message conversation.
    content = [{"type": "text", "text": prompt}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image_input}}
        for image_input in image_inputs
    )
    messages = [{"role": "user", "content": content}]
    result = await img_edit(
        model=resolved.model,
        messages=messages,
        n=n,
        size=size,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        quality=quality,
        response_format=response_format,
        stream=False,
        chat_format=False,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# /v1/files/image — serve locally saved images
# ---------------------------------------------------------------------------


@router.get("/files/video", tags=[_TAG_FILES])
async def serve_video(id: str = Query(..., description="Video file ID")):
    """Serve a locally cached video by file ID."""
    import re

    if not re.fullmatch(r"[0-9a-f\-]{16,36}", id):
        raise ValidationError("Invalid file ID", param="id")

    media_types = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
    }
    for suffix, media_type in media_types.items():
        path = video_files_dir() / f"{id}{suffix}"
        if path.is_file():
            return FileResponse(path, media_type=media_type)

    raise ValidationError(f"Video {id!r} not found", param="id")


@router.get("/files/image", tags=[_TAG_FILES])
async def serve_image(id: str = Query(..., description="Image file ID")):
    """Serve a locally cached image by file ID."""
    import re

    if not re.fullmatch(r"[0-9a-f\-]{16,36}", id):
        raise ValidationError("Invalid file ID", param="id")

    img_dir = image_files_dir()
    for ext in (".jpg", ".png"):
        path = img_dir / f"{id}{ext}"
        if path.exists():
            mime = "image/png" if ext == ".png" else "image/jpeg"
            return FileResponse(path, media_type=mime)

    raise ValidationError(f"Image {id!r} not found", param="id")


@router.get("/media/images/{asset_id}", tags=[_TAG_FILES])
async def serve_archived_image(asset_id: str):
    """Compatibility alias for archived image assets."""
    return await serve_image(id=asset_id)


@router.get("/media/videos/{asset_id}", tags=[_TAG_FILES])
async def serve_archived_video(asset_id: str):
    """Compatibility alias for archived video assets."""
    return await serve_video(id=asset_id)


__all__ = ["router"]
