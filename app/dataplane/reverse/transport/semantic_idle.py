"""Semantic idle timeout helpers for upstream SSE streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any, TypeVar

import orjson

from app.platform.errors import StreamIdleTimeout


T = TypeVar("T")

_GENERATED_DELTA_EVENTS = frozenset(
    {
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
        "response.refusal.delta",
        "response.function_call_arguments.delta",
        "response.custom_tool_call_input.delta",
    }
)

_GENERATED_OUTPUT_ITEM_TYPES = frozenset(
    {
        "code_interpreter_call",
        "custom_tool_call",
        "file_search_call",
        "function_call",
        "image_generation_call",
        "mcp_call",
        "mcp_approval_request",
        "mcp_approval_response",
        "mcp_list_tools",
        "message",
        "reasoning",
        "shell_call",
        "web_search_call",
    }
)


def _close_iterator(iterator: Any) -> Any:
    close = getattr(iterator, "aclose", None)
    return close() if close is not None else None


async def _close_iterator_safely(iterator: Any) -> None:
    close_result = _close_iterator(iterator)
    if close_result is None:
        return
    try:
        await close_result
    except Exception:
        return


async def with_semantic_idle_timeout(
    source: AsyncIterable[T],
    timeout_s: float,
    is_activity: Callable[[T], bool],
) -> AsyncIterator[T]:
    """Stop a stream after *timeout_s* without meaningful generated output."""
    if timeout_s <= 0:
        async for item in source:
            yield item
        return

    iterator = source.__aiter__()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await _close_iterator_safely(iterator)
                raise StreamIdleTimeout(timeout_s)
            try:
                item = await asyncio.wait_for(anext(iterator), timeout=remaining)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as exc:
                await _close_iterator_safely(iterator)
                raise StreamIdleTimeout(timeout_s) from exc

            yield item
            try:
                active = is_activity(item)
            except Exception:
                active = False
            if active:
                deadline = loop.time() + timeout_s
    except asyncio.CancelledError:
        await _close_iterator_safely(iterator)
        raise


def _decode_json_payload(value: str | bytes) -> dict[str, Any] | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    line = value.strip()
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]" or not line.startswith("{"):
        return None
    try:
        payload = orjson.loads(line)
    except (orjson.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def is_chat_stream_activity(line: str | bytes) -> bool:
    """Return whether a grok.com chat frame contains generated activity."""
    payload = _decode_json_payload(line)
    if payload is None:
        return False
    result = payload.get("result")
    response = result.get("response") if isinstance(result, dict) else None
    if not isinstance(response, dict):
        return False
    if str(response.get("token") or ""):
        return True
    for key in (
        "cardAttachment",
        "webSearchResults",
        "xSearchResults",
        "codeExecutionResult",
    ):
        if response.get(key):
            return True
    if response.get("messageTag") == "tool_usage_card":
        return any(response.get(key) for key in ("toolUsageCardId", "rolloutId", "messageStepId"))
    return False


def is_console_response_activity(item: tuple[str, str]) -> bool:
    """Return whether a Console Responses event contains generated activity."""
    if not isinstance(item, tuple) or len(item) != 2:
        return False
    event_type, data = item
    payload = _decode_json_payload(data)
    if payload is None:
        return False

    kind = str(payload.get("type") or event_type or "").strip()
    if kind in _GENERATED_DELTA_EVENTS:
        return bool(str(payload.get("delta") or ""))
    if kind not in {"response.output_item.added", "response.output_item.done"}:
        return False

    item_payload = payload.get("item")
    if not isinstance(item_payload, dict):
        return False
    item_type = str(item_payload.get("type") or "").strip()
    if item_type not in _GENERATED_OUTPUT_ITEM_TYPES:
        return False
    return any(
        str(item_payload.get(key) or "").strip()
        for key in ("id", "call_id", "name")
    )


__all__ = [
    "is_chat_stream_activity",
    "is_console_response_activity",
    "with_semantic_idle_timeout",
]