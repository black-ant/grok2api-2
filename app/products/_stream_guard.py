"""Guard against upstream streams that end without their terminal event.

grok.com chat streams signal completion through ``isSoftStop``/``finalMetadata``
frames or a ``[DONE]`` line. When the socket just closes, synthesising a normal
finish makes clients treat a truncated answer as complete, so the failure is
reported instead. The check is opt-in because a stream may legitimately end right
after its last token on some upstream variants.
"""

from __future__ import annotations

from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamStreamIncompleteError

_CONFIG_KEY = "chat.require_stream_terminal"


def require_stream_terminal() -> bool:
    """Return whether truncated upstream streams must be reported as errors."""
    try:
        return get_config().get_bool(_CONFIG_KEY, False)
    except Exception:
        return False


def enforce_stream_terminal(completed: bool, *, protocol: str) -> None:
    """Raise when the guard is enabled and the terminal event was never seen."""
    if completed or not require_stream_terminal():
        return
    raise UpstreamStreamIncompleteError(protocol)


__all__ = ["enforce_stream_terminal", "require_stream_terminal"]
