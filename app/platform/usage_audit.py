"""Normalized usage and audit records for the local JSONL ledger."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Iterable, Mapping

import orjson


class AuditOperation(StrEnum):
    RESPONSES = "responses"
    CHAT = "chat"
    MESSAGES = "messages"
    IMAGE = "image"
    IMAGE_EDIT = "image_edit"
    VIDEO = "video"


class UsageSource(StrEnum):
    UPSTREAM = "upstream"
    ESTIMATED = "estimated"
    NONE = "none"


TRACKED_OPERATIONS = frozenset(item.value for item in AuditOperation)
SUPPORTED_PERIODS = frozenset({"1h", "24h", "7d", "30d"})
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _number(value: object) -> int | float:
    try:
        return max(0, round(float(value or 0), 2))
    except (TypeError, ValueError, OverflowError):
        return 0


def _first(value: Mapping[str, Any], *keys: str) -> object | None:
    for key in keys:
        if value.get(key) is not None:
            return value[key]
    return None


def _usage_candidate(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    prompt_details = value.get("prompt_tokens_details")
    completion_details = value.get("completion_tokens_details")
    output_details = value.get("output_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, Mapping) else {}
    completion_details = completion_details if isinstance(completion_details, Mapping) else {}
    output_details = output_details if isinstance(output_details, Mapping) else {}
    cached = _first(value, "cached_input_tokens", "cache_read_input_tokens", "cached_tokens")
    cached = cached if cached is not None else _first(prompt_details, "cached_tokens")
    reasoning = _first(value, "reasoning_tokens")
    reasoning = reasoning if reasoning is not None else _first(completion_details, "reasoning_tokens")
    reasoning = reasoning if reasoning is not None else _first(output_details, "reasoning_tokens")
    fields = {
        "input_tokens": _first(value, "input_tokens", "prompt_tokens"),
        "cached_input_tokens": cached,
        "output_tokens": _first(value, "output_tokens", "completion_tokens"),
        "reasoning_tokens": reasoning,
        "total_tokens": _first(value, "total_tokens"),
    }
    return {key: _int(raw) for key, raw in fields.items() if raw is not None}


def _merge(target: dict[str, int], value: Mapping[str, int]) -> None:
    for key, item in value.items():
        if key not in target or item > 0:
            target[key] = item


def _sse_objects(body: str) -> Iterable[Mapping[str, Any]]:
    for line in body.splitlines():
        if not line.strip().startswith("data:"):
            continue
        payload = line.split(":", 1)[1].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            value = orjson.loads(payload)
        except orjson.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            yield value


def extract_usage(value: object) -> tuple[dict[str, int], UsageSource]:
    objects: Iterable[Mapping[str, Any]]
    if isinstance(value, Mapping):
        objects = (value,)
    elif isinstance(value, str):
        objects = _sse_objects(value)
    else:
        objects = ()
    merged: dict[str, int] = {}
    for item in objects:
        candidates: list[object] = []
        if item.get("usage") is not None:
            candidates.append(item["usage"])
        response = item.get("response")
        if isinstance(response, Mapping) and response.get("usage") is not None:
            candidates.append(response["usage"])
        message = item.get("message")
        if isinstance(message, Mapping) and message.get("usage") is not None:
            candidates.append(message["usage"])
        for candidate in candidates:
            _merge(merged, _usage_candidate(candidate))
    if not merged:
        return {}, UsageSource.NONE
    merged.setdefault("total_tokens", merged.get("input_tokens", 0) + merged.get("output_tokens", 0))
    return merged, UsageSource.ESTIMATED


def extract_error_code(value: object) -> str | None:
    objects: Iterable[Mapping[str, Any]]
    if isinstance(value, Mapping):
        objects = (value,)
    elif isinstance(value, str):
        objects = _sse_objects(value)
    else:
        objects = ()
    for item in objects:
        code = item.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
        error = item.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            if isinstance(code, str) and code.strip():
                return code.strip()
    return None


def _data_count(value: object) -> int:
    return len(value["data"]) if isinstance(value, Mapping) and isinstance(value.get("data"), list) else 0


def operation_for_path(path: str) -> str | None:
    path = path.rstrip("/") or "/"
    if path.endswith("/responses"):
        return AuditOperation.RESPONSES.value
    if path.endswith("/chat/completions"):
        return AuditOperation.CHAT.value
    if path.endswith("/messages"):
        return AuditOperation.MESSAGES.value
    if path.endswith("/images/generations"):
        return AuditOperation.IMAGE.value
    if path.endswith("/images/edits"):
        return AuditOperation.IMAGE_EDIT.value
    if path.endswith("/videos") or path.endswith("/videos/generations"):
        return AuditOperation.VIDEO.value
    return None


def _routing_summary(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = ("model", "resolved_model", "virtual_model", "model_pool", "pool", "mode_id", "routed_key_tail")
    result = {key: value[key] for key in allowed if value.get(key) is not None}
    fallbacks = value.get("model_fallbacks")
    if isinstance(fallbacks, list):
        result["fallback_count"] = len(fallbacks)
        result["fallbacks"] = [
            {"from": item["from"], "to": item["to"], "status": item.get("status")}
            for item in fallbacks
            if isinstance(item, Mapping) and isinstance(item.get("from"), str) and isinstance(item.get("to"), str)
        ]
    return result


def build_audit_record(
    *,
    request_id: str,
    created_ts: float,
    started_at: str,
    path: str,
    status_code: int,
    duration_ms: int | float,
    response_content_type: str,
    response_body: object,
    response_truncated: bool,
    routing: object,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    context = state.get("request_log_audit")
    context = context if isinstance(context, Mapping) else {}
    operation = context.get("operation") or state.get("request_log_operation") or operation_for_path(path)
    if operation not in TRACKED_OPERATIONS:
        return None
    usage_context = state.get("request_log_usage")
    usage_context = usage_context if isinstance(usage_context, Mapping) else {}
    parsed, parsed_source = extract_usage(response_body)
    explicit = _usage_candidate(usage_context)
    usage = {**parsed, **explicit}
    source = usage_context.get("source")
    usage_source = UsageSource.NONE if not usage else (
        UsageSource(source) if source in {item.value for item in UsageSource} else parsed_source
    )
    for field in _TOKEN_FIELDS:
        usage.setdefault(field, 0)
    usage["total_tokens"] = usage["total_tokens"] or usage["input_tokens"] + usage["output_tokens"]
    streaming = context.get("streaming")
    if streaming is None:
        streaming = state.get("request_log_streaming")
    if streaming is None:
        streaming = response_content_type.lower().startswith("text/event-stream")
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = 500
    error_code = context.get("error_code") or state.get("request_log_error") or extract_error_code(response_body)
    error_code = error_code.strip() if isinstance(error_code, str) and error_code.strip() else None
    success = 200 <= status < 300 and error_code is None
    input_images = _int(usage_context.get("media_input_images"))
    output_images = _int(usage_context.get("media_output_images"))
    output_seconds = _int(usage_context.get("media_output_seconds"))
    if success and operation in {AuditOperation.IMAGE.value, AuditOperation.IMAGE_EDIT.value}:
        output_images = max(output_images, _data_count(response_body))
    if not success:
        output_images = 0
        output_seconds = 0
    routing_value = routing if isinstance(routing, Mapping) else {}
    model = routing_value.get("model") or context.get("model") or ""
    resolved_model = routing_value.get("resolved_model") or context.get("resolved_model") or ""
    return {
        "schema_version": 1,
        "request_id": request_id,
        "created_ts": created_ts,
        "created_at": started_at,
        "operation": operation,
        "provider": str(context.get("provider") or "grok"),
        "model": str(model),
        "resolved_model": str(resolved_model),
        "status_code": status,
        "success": success,
        "streaming": bool(streaming),
        "usage_source": usage_source.value,
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "total_tokens": usage["total_tokens"],
        "media_input_images": input_images,
        "media_output_images": output_images,
        "media_output_seconds": output_seconds,
        "duration_ms": _number(duration_ms),
        "error_code": error_code,
        "routing": _routing_summary(routing),
        "response_truncated": bool(response_truncated),
    }


def period_range(period: str, *, now_ts: float | None = None) -> tuple[float, float, str, str]:
    normalized = (period or "24h").strip().lower()
    if normalized not in SUPPORTED_PERIODS:
        raise ValueError(f"period must be one of: {', '.join(sorted(SUPPORTED_PERIODS))}")
    end = datetime.now(UTC) if now_ts is None else datetime.fromtimestamp(now_ts, UTC)
    duration = {"1h": timedelta(hours=1), "24h": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}[normalized]
    start = end - duration
    return start.timestamp(), end.timestamp(), start.isoformat(), end.isoformat()


def audit_matches(record: Mapping[str, Any], *, start_ts: float, end_ts: float, operation: str | None = None, model: str | None = None, status: int | None = None) -> bool:
    try:
        created_ts = float(record.get("created_ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if not start_ts <= created_ts <= end_ts:
        return False
    if operation and record.get("operation") != operation:
        return False
    if model and model not in {str(record.get("model") or ""), str(record.get("resolved_model") or "")}:
        return False
    return status is None or int(record.get("status_code") or 0) == status


def _bucket() -> dict[str, int | float]:
    return {"requests": 0, "successful_requests": 0, "failed_requests": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0, "media_input_images": 0, "media_output_images": 0, "media_output_seconds": 0, "duration_ms": 0}


def _add(bucket: dict[str, int | float], record: Mapping[str, Any]) -> None:
    bucket["requests"] += 1
    bucket["successful_requests"] += int(bool(record.get("success")))
    bucket["failed_requests"] += int(not record.get("success"))
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "media_input_images", "media_output_images", "media_output_seconds"):
        bucket[key] += _int(record.get(key))
    bucket["duration_ms"] += _number(record.get("duration_ms"))


def _finish(bucket: dict[str, int | float]) -> dict[str, int | float]:
    result = dict(bucket)
    requests = int(bucket["requests"])
    result["average_duration_ms"] = round(float(bucket["duration_ms"]) / requests, 2) if requests else 0
    result["success_rate"] = round(int(bucket["successful_requests"]) * 100 / requests, 2) if requests else 0
    return result


def summarize_audits(records: Iterable[Mapping[str, Any]], *, period: str = "24h", operation: str | None = None, model: str | None = None, status: int | None = None, now_ts: float | None = None) -> dict[str, Any]:
    start_ts, end_ts, start_at, end_at = period_range(period, now_ts=now_ts)
    total = _bucket()
    by_operation: dict[str, dict[str, int | float]] = defaultdict(_bucket)
    by_model: dict[str, dict[str, int | float]] = defaultdict(_bucket)
    coverage = {item.value: 0 for item in UsageSource}
    for record in records:
        if not audit_matches(record, start_ts=start_ts, end_ts=end_ts, operation=operation, model=model, status=status):
            continue
        _add(total, record)
        operation_name = str(record.get("operation") or "unknown")
        model_name = str(record.get("model") or record.get("resolved_model") or "unknown")
        _add(by_operation[operation_name], record)
        _add(by_model[model_name], record)
        source = str(record.get("usage_source") or UsageSource.NONE.value)
        coverage[source] = coverage.get(source, 0) + 1
    requests = int(total["requests"])
    return {
        "period": period,
        "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "range": {"start": start_at, "end": end_at},
        "usage": _finish(total),
        "coverage": {"upstream_usage_requests": coverage[UsageSource.UPSTREAM.value], "estimated_usage_requests": coverage[UsageSource.ESTIMATED.value], "missing_usage_requests": coverage[UsageSource.NONE.value]},
        "pricing": {"available": False, "priced_requests": 0, "unpriced_requests": requests},
        "by_operation": [{"operation": key, **_finish(value)} for key, value in sorted(by_operation.items())],
        "by_model": [{"model": key, **_finish(value)} for key, value in sorted(by_model.items())],
    }


__all__ = ["AuditOperation", "UsageSource", "TRACKED_OPERATIONS", "SUPPORTED_PERIODS", "audit_matches", "build_audit_record", "extract_error_code", "extract_usage", "operation_for_path", "period_range", "summarize_audits"]