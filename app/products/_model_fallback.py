"""Shared policy for virtual-model fallback after upstream rate limits."""

from __future__ import annotations

from typing import Any

from app.control.model.cooldown import blocked_models


def fallback_limit(config, candidates: tuple[str, ...], *, force_token: str | None = None) -> int:
    if force_token or not config.get_bool("features.auto_model_fallback", True):
        return 0
    configured = config.get_int("retry.model_fallback_max_retries", 5)
    return max(0, min(configured, len(candidates)))


def cooldown_seconds(config) -> int:
    return max(0, config.get_int("retry.model_fallback_cooldown_sec", 120))


def max_cooldown_seconds(config) -> int:
    configured = config.get_int("retry.model_fallback_max_cooldown_sec", 1800)
    return max(cooldown_seconds(config), configured)


def jitter_ratio(config) -> float:
    return max(0.0, config.get_float("retry.model_fallback_jitter_ratio", 0.1))


def next_fallback_candidate(
    candidates: tuple[str, ...],
    start_index: int,
    limit: int,
) -> tuple[int, str] | None:
    blocked = blocked_models()
    upper_bound = min(len(candidates), max(0, limit))
    for index in range(max(0, start_index), upper_bound):
        candidate = candidates[index]
        if candidate not in blocked:
            return index, candidate
    return None


def record_fallback(
    routing: dict[str, Any] | None,
    *,
    from_model: str,
    to_model: str,
    status: int,
) -> None:
    if routing is None:
        return
    history = routing.setdefault("model_fallbacks", [])
    if isinstance(history, list):
        history.append({"from": from_model, "to": to_model, "status": status})
    routing["resolved_model"] = to_model


__all__ = [
    "cooldown_seconds",
    "fallback_limit",
    "jitter_ratio",
    "max_cooldown_seconds",
    "next_fallback_candidate",
    "record_fallback",
]