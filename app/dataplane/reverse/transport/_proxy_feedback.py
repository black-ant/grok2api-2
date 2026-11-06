"""Shared helper: map an UpstreamError to the correct ProxyFeedbackKind.

All transport modules (assets, media, livekit, imagine_ws …) use this so the
mapping stays consistent and clearance bundles are properly invalidated.

Rules
-----
401  → UNAUTHORIZED  (invalidates clearance bundle)
403  → CHALLENGE     (invalidates clearance bundle — treat all 403s as potential CF)
403 + content refusal → NEUTRAL (request-level outcome, no proxy action)
429  → RATE_LIMITED
≥500 → UPSTREAM_5XX
else → TRANSPORT_ERROR
"""

from app.platform.errors import UpstreamError
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind


def upstream_feedback(exc: UpstreamError) -> ProxyFeedback:
    """Return a ``ProxyFeedback`` for an ``UpstreamError`` response."""
    from app.dataplane.reverse.protocol.xai_usage import (
        is_content_refusal_error,
        is_invalid_credentials_error,
    )

    status = exc.status or 0
    # The account answered fine and only the request content was rejected: keep
    # the clearance bundle and the current egress node untouched.
    if is_content_refusal_error(exc) and not is_invalid_credentials_error(exc):
        return ProxyFeedback(kind=ProxyFeedbackKind.NEUTRAL, status_code=status or None)
    if status == 401:
        kind = ProxyFeedbackKind.UNAUTHORIZED
    elif status == 403:
        kind = ProxyFeedbackKind.CHALLENGE
    elif status == 429:
        kind = ProxyFeedbackKind.RATE_LIMITED
    elif status >= 500:
        kind = ProxyFeedbackKind.UPSTREAM_5XX
    else:
        kind = ProxyFeedbackKind.TRANSPORT_ERROR
    return ProxyFeedback(kind=kind, status_code=status or None)


__all__ = ["upstream_feedback"]
