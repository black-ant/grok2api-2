"""Process-local model health state for rate-limit fallback and recovery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import random
from threading import RLock
from time import monotonic


class ModelAdmission(str, Enum):
    """Admission result for a model request."""

    NORMAL = "normal"
    PROBE = "probe"
    BLOCKED = "blocked"


@dataclass(slots=True)
class _ModelState:
    consecutive_rate_limits: int = 0
    cooldown_until: float = 0.0
    probe_in_flight: bool = False


_LOCK = RLock()
_STATES: dict[str, _ModelState] = {}


def _normalize(model_name: str) -> str:
    return str(model_name or "").strip()


def blocked_models() -> frozenset[str]:
    now = monotonic()
    with _LOCK:
        return frozenset(
            name
            for name, state in _STATES.items()
            if state.cooldown_until > now or state.probe_in_flight
        )


def admit_model(model_name: str) -> ModelAdmission:
    """Allow normal traffic or reserve one recovery probe for a model."""
    name = _normalize(model_name)
    if not name:
        return ModelAdmission.NORMAL
    with _LOCK:
        state = _STATES.get(name)
        if state is None:
            return ModelAdmission.NORMAL
        now = monotonic()
        if state.cooldown_until > now or state.probe_in_flight:
            return ModelAdmission.BLOCKED
        state.probe_in_flight = True
        return ModelAdmission.PROBE


def mark_rate_limited(
    model_name: str,
    cooldown_sec: int | float,
    *,
    max_cooldown_sec: int | float | None = None,
    retry_after_sec: int | float | None = None,
    jitter_ratio: int | float = 0.0,
) -> float:
    """Record a 429 using bounded exponential backoff and return its delay."""
    name = _normalize(model_name)
    seconds = max(0.0, float(cooldown_sec))
    retry_after = max(0.0, float(retry_after_sec or 0.0))
    if not name:
        return 0.0
    if seconds <= 0 and retry_after <= 0:
        with _LOCK:
            _STATES.pop(name, None)
        return 0.0

    configured_max = (
        seconds
        if max_cooldown_sec is None or float(max_cooldown_sec) <= 0
        else max(seconds, float(max_cooldown_sec))
    )
    with _LOCK:
        now = monotonic()
        state = _STATES.setdefault(name, _ModelState())
        state.consecutive_rate_limits += 1
        exponent = min(state.consecutive_rate_limits - 1, 30)
        local_delay = (
            min(configured_max, seconds * (2**exponent))
            if seconds > 0
            else 0.0
        )
        if local_delay > 0:
            ratio = max(0.0, float(jitter_ratio))
            if ratio > 0:
                local_delay = min(
                    configured_max,
                    local_delay + random.uniform(0.0, local_delay * ratio),
                )
        delay = max(local_delay, retry_after)
        state.cooldown_until = max(state.cooldown_until, now + delay)
        state.probe_in_flight = False
        return delay


def mark_model_success(model_name: str) -> None:
    """Clear all model failure state after a successful request or probe."""
    name = _normalize(model_name)
    if not name:
        return
    with _LOCK:
        _STATES.pop(name, None)


def release_probe(model_name: str) -> None:
    """Release a probe reservation after a non-rate-limit failure."""
    name = _normalize(model_name)
    if not name:
        return
    with _LOCK:
        state = _STATES.get(name)
        if state is not None:
            state.probe_in_flight = False


def clear_rate_limit(model_name: str) -> None:
    """Backward-compatible alias for clearing model failure state."""
    mark_model_success(model_name)


def reset_rate_limits() -> None:
    with _LOCK:
        _STATES.clear()


__all__ = [
    "ModelAdmission",
    "admit_model",
    "blocked_models",
    "clear_rate_limit",
    "mark_model_success",
    "mark_rate_limited",
    "release_probe",
    "reset_rate_limits",
]
