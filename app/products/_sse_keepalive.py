"""SSE keepalive helpers for long-lived streaming responses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncGenerator

from app.platform.config.snapshot import get_config


_CONFIG_KEY = "chat.stream_keepalive"
_DEFAULT_INTERVAL_S = 15.0
KEEPALIVE_FRAME = ": heartbeat\n\n"


def keepalive_interval_s() -> float:
    """Return the configured keepalive interval in seconds (0 disables it)."""
    try:
        value = get_config().get_float(_CONFIG_KEY, _DEFAULT_INTERVAL_S)
    except Exception:
        return _DEFAULT_INTERVAL_S
    return max(0.0, float(value))


async def with_keepalive(
    source: AsyncIterable[str],
    interval_s: float,
    frame: str = KEEPALIVE_FRAME,
) -> AsyncGenerator[str, None]:
    """Yield *frame* whenever *source* stays silent for *interval_s* seconds.

    The pending read stays alive across keepalive frames, so a slow upstream is
    never cancelled by the timer itself.
    """
    if interval_s <= 0:
        async for item in source:
            yield item
        return

    iterator = source.__aiter__()
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(iterator))
            done, _ = await asyncio.wait({pending}, timeout=interval_s)
            if not done:
                yield frame
                continue
            finished, pending = pending, None
            try:
                item = finished.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


def keepalive_stream(stream: AsyncIterable[str]) -> AsyncIterable[str]:
    """Apply the configured keepalive to *stream* (no-op when disabled)."""
    interval_s = keepalive_interval_s()
    if interval_s <= 0:
        return stream
    return with_keepalive(stream, interval_s)


__all__ = [
    "KEEPALIVE_FRAME",
    "keepalive_interval_s",
    "keepalive_stream",
    "with_keepalive",
]
