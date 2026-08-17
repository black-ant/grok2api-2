"""XAI rate-limits API protocol — fetch live quota data per mode."""

import asyncio
from datetime import datetime, timezone

import orjson

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms
from app.control.account.quota_defaults import (
    IMAGE_EDIT_MODE_ID,
    IMAGE_PRO_MODE_ID,
    IMAGE_VIDEO_720P_MODE_ID,
    IMAGE_VIDEO_MODE_ID,
    IMAGE_QUOTA_WINDOW_SECONDS,
)


# ---------------------------------------------------------------------------
# Mode → request model name
# ---------------------------------------------------------------------------

# Each mode requires a separate POST request.
# The API returns a flat single-mode quota object per call.
_MODE_NAMES: dict[int, str] = {
    0: "auto",
    1: "fast",
    2: "expert",
    3: "heavy",
    4: "grok-420-computer-use-sa",
}

# Default window durations used as fallback when API call fails.
_DEFAULT_WINDOW_SECS: dict[int, int] = {
    0: 7_200,  # auto   — 2 h  (super/heavy only)
    1: 86_400,  # fast   — 24 h (basic; real value overrides for super/heavy)
    2: 7_200,  # expert — 2 h  (super/heavy only)
    3: 7_200,  # heavy  — 2 h  (heavy-pool only)
    4: 7_200,  # grok_4_3 — 2 h  (super/heavy only)
}

_IMAGINE_QUOTA_FIELDS: tuple[tuple[str, int | None], ...] = (
    ('image', None),
    ('imagePro', IMAGE_PRO_MODE_ID),
    ('imageEdit', IMAGE_EDIT_MODE_ID),
    ('video', IMAGE_VIDEO_MODE_ID),
    ('video720p', IMAGE_VIDEO_720P_MODE_ID),
)


def _build_payload(mode_name: str) -> bytes:
    """Build rate-limits request payload: {"modelName": "fast"}"""
    return orjson.dumps({"modelName": mode_name})


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def parse_rate_limits(
    body: dict, *, default_window_seconds: int = 72_000
) -> dict | None:
    """Parse flat rate-limits response.

    Expected format::

        {
            "windowSizeSeconds": 72000,
            "remainingQueries":  20,
            "totalQueries":      20,
            "lowEffortRateLimits":  null,
            "highEffortRateLimits": null
        }

    Returns a dict with keys ``remaining``, ``total``, ``window_seconds``
    or ``None`` if the required fields are absent.
    """
    remaining = body.get("remainingQueries")
    if remaining is None:
        return None
    total = body.get("totalQueries")
    window_secs = body.get("windowSizeSeconds")
    return {
        "remaining": int(remaining),
        "total": int(total) if total is not None else int(remaining),
        "window_seconds": int(window_secs) if window_secs else default_window_seconds,
    }


# ---------------------------------------------------------------------------
# Imagine quota group parser
# ---------------------------------------------------------------------------


def _parse_imagine_reset_at(value: object, synced_at: int, window_seconds: int, remaining: int, available: bool) -> int | None:
    if value:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                text = value.strip().replace('Z', '+00:00')
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return int(parsed.timestamp() * 1000)
            except ValueError:
                return None
    if not available or remaining <= 0:
        return synced_at + window_seconds * 1000
    return None


def parse_imagine_quota(body: dict, *, synced_at: int | None = None) -> dict[int, object] | None:
    if not isinstance(body, dict):
        return None
    synced = synced_at if synced_at is not None else now_ms()
    windows: dict[int, object] = {}
    for field, mode_id in _IMAGINE_QUOTA_FIELDS:
        if field not in body:
            return None
        product = body[field]
        if product is None:
            continue
        if not isinstance(product, dict):
            return None
        available = product.get('available')
        if not isinstance(available, bool):
            return None
        if mode_id is None:
            continue
        remaining_raw = product.get('remainingQueries')
        window_raw = product.get('windowSizeSeconds')
        if available and (remaining_raw is None or window_raw is None):
            return None
        try:
            remaining = max(0, int(remaining_raw)) if remaining_raw is not None else 0
            window_seconds = (
                int(window_raw)
                if window_raw is not None
                else IMAGE_QUOTA_WINDOW_SECONDS
            )
        except (TypeError, ValueError):
            return None
        if window_seconds <= 0:
            return None
        reset_at = _parse_imagine_reset_at(
            product.get('nextAvailableAt'),
            synced,
            window_seconds,
            remaining,
            available,
        )
        from app.control.account.enums import QuotaSource
        from app.control.account.models import QuotaWindow

        windows[mode_id] = QuotaWindow(
            remaining=remaining,
            total=0,
            window_seconds=window_seconds,
            reset_at=reset_at,
            synced_at=synced,
            source=QuotaSource.REAL,
        )
    return windows


# ---------------------------------------------------------------------------
# QuotaWindow builder
# ---------------------------------------------------------------------------


def _to_quota_window(data: dict, synced_at: int) -> object:
    from app.control.account.models import QuotaWindow
    from app.control.account.enums import QuotaSource

    ws = data["window_seconds"]
    return QuotaWindow(
        remaining=data["remaining"],
        total=data["total"],
        window_seconds=ws,
        reset_at=synced_at + ws * 1000,  # estimated end of current window
        synced_at=synced_at,
        source=QuotaSource.REAL,
    )


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


async def _do_fetch(token: str, mode_name: str) -> dict:
    """POST the rate-limits endpoint for one mode and return parsed JSON body."""
    from app.dataplane.reverse.transport.http import post_json
    from app.dataplane.proxy import get_proxy_runtime
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    try:
        body = await post_json(
            "https://grok.com/rest/rate-limits",
            token,
            _build_payload(mode_name),
            lease=lease,
            timeout_s=20.0,
        )
        await proxy.feedback(
            lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200)
        )
        return body
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        kind = _proxy_feedback_kind_for_error(exc, status=status)
        await proxy.feedback(lease, ProxyFeedback(kind=kind, status_code=status))
        raise


async def _do_fetch_imagine_quota(token: str) -> dict:
    from app.dataplane.reverse.transport.http import post_json
    from app.dataplane.proxy import get_proxy_runtime
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    try:
        body = await post_json(
            'https://grok.com/rest/media/imagine/quota_info',
            token,
            b'{}',
            lease=lease,
            timeout_s=20.0,
            referer='https://grok.com/imagine',
        )
        await proxy.feedback(
            lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200)
        )
        return body
    except Exception as exc:
        status = getattr(exc, 'status', None) or getattr(exc, 'status_code', None)
        kind = _proxy_feedback_kind_for_error(exc, status=status)
        await proxy.feedback(lease, ProxyFeedback(kind=kind, status_code=status))
        raise


async def _fetch_one(token: str, mode_id: int) -> object | None:
    """Fetch quota window for a single mode. Returns QuotaWindow or None."""
    mode_name = _MODE_NAMES.get(mode_id, "auto")
    try:
        body = await asyncio.wait_for(_do_fetch(token, mode_name), timeout=25.0)
    except asyncio.TimeoutError:
        logger.debug(
            "rate-limits fetch timed out: token={}... mode={}", token[:10], mode_name
        )
        return None
    except Exception as exc:
        if is_invalid_credentials_error(exc):
            raise
        logger.debug(
            "rate-limits fetch failed: token={}... mode={} error={}",
            token[:10],
            mode_name,
            exc,
        )
        return None

    data = parse_rate_limits(
        body,
        default_window_seconds=_DEFAULT_WINDOW_SECS.get(mode_id, 72_000),
    )
    if data is None:
        logger.debug(
            "rate-limits response missing quota fields: token={}... mode={} body={}",
            token[:10],
            mode_name,
            body,
        )
        return None

    return _to_quota_window(data, now_ms())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_all_quotas(
    token: str, mode_ids: tuple[int, ...] | None = None
) -> dict[int, object] | None:
    """Fetch quota windows for the requested modes concurrently.

    ``mode_ids`` defaults to ``(0, 1, 2, 3, 4)``. Returns ``{mode_id: QuotaWindow}``
    for every mode that responded successfully, or ``None`` if every requested
    mode failed.
    """
    import asyncio

    requested = mode_ids or (0, 1, 2, 3, 4)
    results = await asyncio.gather(
        *(_fetch_one(token, mode_id) for mode_id in requested), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception) and is_invalid_credentials_error(result):
            raise result
    windows = {
        mode_id: win
        for mode_id, win in zip(requested, results, strict=False)
        if win is not None and not isinstance(win, Exception)
    }
    return windows if windows else None


async def fetch_mode_quota(token: str, mode_id: int) -> object | None:
    """Fetch the quota window for a single mode. Returns QuotaWindow or None."""
    return await _fetch_one(token, mode_id)


async def fetch_imagine_quota_group(token: str) -> dict[int, object] | None:
    try:
        body = await asyncio.wait_for(_do_fetch_imagine_quota(token), timeout=25.0)
    except asyncio.TimeoutError:
        logger.debug('imagine quota fetch timed out: token={}...', token[:10])
        return None
    except Exception as exc:
        if is_invalid_credentials_error(exc):
            raise
        logger.debug(
            'imagine quota fetch failed: token={}... error={}', token[:10], exc
        )
        return None
    return parse_imagine_quota(body)


def is_invalid_credentials_body(body: str) -> bool:
    """Return whether *body* contains a Grok invalid/blocked account marker."""
    text = str(body or "").lower()
    return (
        "invalid-credentials" in text
        or "bad-credentials" in text
        or "failed to look up session id" in text
        or "blocked-user" in text
        or "email-domain-rejected" in text
        or "session not found" in text
        or "account suspended" in text
        or "token revoked" in text
        or "token expired" in text
    )


def is_invalid_credentials_error(exc: BaseException) -> bool:
    """Return whether *exc* indicates the account is invalid or blocked."""
    if not isinstance(exc, UpstreamError):
        return False
    if exc.status not in (400, 401, 403):
        return False
    return is_invalid_credentials_body(str(exc.details.get("body", "") or ""))


def _proxy_feedback_kind_for_error(
    exc: BaseException,
    *,
    status: int | None,
):
    """Map quota-fetch failures to proxy feedback without burning healthy clearance."""
    from app.control.proxy.models import ProxyFeedbackKind

    # Invalid or blocked accounts are account problems, not proxy problems.
    if is_invalid_credentials_error(exc):
        return ProxyFeedbackKind.FORBIDDEN

    if status == 429:
        return ProxyFeedbackKind.RATE_LIMITED
    if status == 403:
        return ProxyFeedbackKind.CHALLENGE
    if status == 401:
        return ProxyFeedbackKind.UNAUTHORIZED
    if status and status >= 500:
        return ProxyFeedbackKind.UPSTREAM_5XX
    return ProxyFeedbackKind.TRANSPORT_ERROR


__all__ = [
    "parse_rate_limits",
    "fetch_all_quotas",
    "fetch_mode_quota",
    "is_invalid_credentials_body",
    "is_invalid_credentials_error",
    'parse_imagine_quota',
    'fetch_imagine_quota_group',
]
