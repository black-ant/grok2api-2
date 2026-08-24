"""Virtual model aliases with weighted pool routing and recovery promotion."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import gcd
from threading import RLock
from time import time
from typing import Any, Iterable

from app.platform.config.snapshot import get_config

from . import registry
from .enums import Capability
from .spec import ModelSpec


DEFAULT_STABLE_RATIO = 95
DEFAULT_DEGRADED_RATIO = 5


@dataclass(frozen=True)
class ModelPoolConfig:
    stable: tuple[str, ...]
    degraded: tuple[str, ...]
    stable_ratio: int = DEFAULT_STABLE_RATIO
    degraded_ratio: int = DEFAULT_DEGRADED_RATIO

    @property
    def candidates(self) -> tuple[str, ...]:
        return (*self.stable, *self.degraded)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stable": list(self.stable),
            "degraded": list(self.degraded),
            "stable_ratio": self.stable_ratio,
            "degraded_ratio": self.degraded_ratio,
        }


DEFAULT_ALIAS_CONFIG: dict[str, ModelPoolConfig] = {
    "FREE": ModelPoolConfig(
        stable=("grok-4.3-console",),
        degraded=("grok-4.20-0309-console",),
    ),
    "SUPER": ModelPoolConfig(
        stable=("grok-4.20-auto",),
        degraded=("grok-4.3-beta",),
    ),
}

_CORE_ALIAS_CAPABILITIES: dict[str, Capability] = {
    "FREE": Capability.CONSOLE_CHAT,
}


@dataclass(frozen=True)
class ModelResolution:
    """Resolved model request.

    ``requested_model`` is the client-facing value. ``model`` and ``spec`` are
    the real model selected for downstream routing. ``pool`` records the runtime
    pool used for this request so callers can include it in request diagnostics.
    """

    requested_model: str
    model: str
    spec: ModelSpec
    is_virtual: bool
    candidates: tuple[str, ...] = ()
    pool: str = "stable"


@dataclass(slots=True)
class _AliasRuntime:
    signature: tuple[Any, ...]
    dispatch_cursor: int = 0
    stable_cursor: int = 0
    degraded_cursor: int = 0
    promoted: set[str] = field(default_factory=set)
    demoted: set[str] = field(default_factory=set)
    started_at: float = field(default_factory=time)
    selection_count: int = 0
    stable_requests: int = 0
    degraded_requests: int = 0
    model_requests: dict[str, int] = field(default_factory=dict)
    model_last_used_at: dict[str, float] = field(default_factory=dict)
    recent_routes: list[dict[str, Any]] = field(default_factory=list)
    pool_event_count: int = 0
    pool_events: list[dict[str, Any]] = field(default_factory=list)


_ROUTING_LOCK = RLock()
_RUNTIMES: dict[str, _AliasRuntime] = {}
_MAX_POOL_EVENTS = 50


def _as_model_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item).strip()
        if name and name not in seen:
            result.append(name)
            seen.add(name)
    return tuple(result)


def _ratio(value: object, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _parse_pool(value: object) -> ModelPoolConfig:
    if isinstance(value, dict):
        stable = _unique(_as_model_list(value.get("stable")))
        degraded = _unique(
            item for item in _as_model_list(value.get("degraded")) if item not in stable
        )
        stable_ratio = _ratio(value.get("stable_ratio"), DEFAULT_STABLE_RATIO)
        degraded_ratio = _ratio(value.get("degraded_ratio"), DEFAULT_DEGRADED_RATIO)
    else:
        # Backward compatibility: the old ordered array is treated as a stable
        # pool. Runtime 429 state can still move individual models to degraded.
        stable = _unique(_as_model_list(value))
        degraded = ()
        stable_ratio = DEFAULT_STABLE_RATIO
        degraded_ratio = DEFAULT_DEGRADED_RATIO

    if stable_ratio + degraded_ratio <= 0:
        stable_ratio = DEFAULT_STABLE_RATIO
        degraded_ratio = DEFAULT_DEGRADED_RATIO

    return ModelPoolConfig(
        stable=stable,
        degraded=degraded,
        stable_ratio=stable_ratio,
        degraded_ratio=degraded_ratio,
    )


def _candidate_is_usable(alias_name: str, candidate: str) -> bool:
    spec = registry.get(candidate)
    if spec is None or not spec.enabled or not spec.supported_in_api:
        return False
    required_capability = _CORE_ALIAS_CAPABILITIES.get(alias_name)
    return required_capability is None or bool(spec.capability & required_capability)


def _sanitize_pool(alias_name: str, config: ModelPoolConfig) -> ModelPoolConfig:
    stable = _unique(
        candidate
        for candidate in config.stable
        if _candidate_is_usable(alias_name, candidate)
    )
    degraded = _unique(
        candidate
        for candidate in config.degraded
        if candidate not in stable and _candidate_is_usable(alias_name, candidate)
    )
    return ModelPoolConfig(
        stable=stable,
        degraded=degraded,
        stable_ratio=config.stable_ratio,
        degraded_ratio=config.degraded_ratio,
    )


def alias_supported_in_api(alias_name: str) -> bool | None:
    """Return API availability for a virtual alias, or ``None`` for real models."""
    config = alias_configs().get(alias_name)
    if config is None:
        return None
    return any(_candidate_is_usable(alias_name, candidate) for candidate in config.candidates)


def is_resolution_usable(resolution: ModelResolution) -> bool:
    """Ensure a virtual alias resolved to a candidate matching its contract."""
    if not resolution.is_virtual:
        return True
    return _candidate_is_usable(resolution.requested_model, resolution.model)


def _raw_aliases() -> dict[str, object]:
    raw = get_config("models.aliases", {})
    return raw if isinstance(raw, dict) else {}


def alias_configs() -> dict[str, ModelPoolConfig]:
    """Return normalized stable/degraded pool configuration for each alias.

    Core aliases always have a fallback so an empty or partially cleared
    ``models.aliases`` cannot silently disable FREE/SUPER routing.
    """

    result: dict[str, ModelPoolConfig] = {}
    for virtual_model, value in _raw_aliases().items():
        name = str(virtual_model).strip()
        if name:
            result[name] = _sanitize_pool(name, _parse_pool(value))
    for name, default in DEFAULT_ALIAS_CONFIG.items():
        current = result.get(name)
        if current is None or not current.candidates:
            result[name] = default
    return result


def alias_config_map() -> dict[str, dict[str, Any]]:
    """Return the normalized pool configuration used by the admin API."""

    return {name: config.as_dict() for name, config in alias_configs().items()}


def normalize_alias_config(value: object) -> dict[str, dict[str, Any]]:
    """Normalize an admin payload while accepting both old arrays and new pools."""

    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for virtual_model, pool in value.items():
        name = str(virtual_model).strip()
        if name:
            result[name] = _sanitize_pool(name, _parse_pool(pool)).as_dict()
    return result


def alias_map() -> dict[str, list[str]]:
    """Return each alias as one flattened candidate list for legacy callers."""

    return {
        name: list(config.candidates)
        for name, config in alias_configs().items()
    }


def is_virtual_model(model_name: str) -> bool:
    return model_name in alias_configs()


def _runtime_for(alias_name: str, config: ModelPoolConfig) -> _AliasRuntime:
    signature = (
        config.stable,
        config.degraded,
        config.stable_ratio,
        config.degraded_ratio,
    )
    runtime = _RUNTIMES.get(alias_name)
    if runtime is None or runtime.signature != signature:
        runtime = _AliasRuntime(signature=signature)
        _RUNTIMES[alias_name] = runtime
    return runtime


def _effective_pools(
    config: ModelPoolConfig,
    runtime: _AliasRuntime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    stable = [
        candidate
        for candidate in config.stable
        if candidate not in runtime.demoted
    ]
    stable.extend(
        candidate
        for candidate in config.degraded
        if candidate in runtime.promoted and candidate not in runtime.demoted
    )

    degraded = [
        candidate
        for candidate in config.degraded
        if candidate not in runtime.promoted
    ]
    degraded.extend(
        candidate
        for candidate in config.stable
        if candidate in runtime.demoted and candidate not in degraded
    )
    return _unique(stable), _unique(degraded)


def _candidate_is_available(
    candidate: str,
    *,
    available_pools: frozenset[str] | None,
    is_available,
    blocked: frozenset[str],
    allow_blocked: bool,
    ignore_pool_availability: bool = False,
) -> bool:
    if candidate in blocked and not allow_blocked:
        return False
    spec = registry.get(candidate)
    if spec is None or not spec.enabled:
        return False
    if (
        not ignore_pool_availability
        and available_pools is not None
        and is_available is not None
        and not is_available(spec, available_pools)
    ):
        return False
    return True


def _pick_from_pool(
    candidates: tuple[str, ...],
    *,
    cursor: int,
    runtime: _AliasRuntime,
    cursor_name: str,
    available_pools: frozenset[str] | None,
    is_available,
    blocked: frozenset[str],
    allow_blocked: bool,
    ignore_pool_availability: bool = False,
    advance: bool,
) -> tuple[str, int] | None:
    if not candidates:
        return None
    for offset in range(len(candidates)):
        index = (cursor + offset) % len(candidates)
        candidate = candidates[index]
        if _candidate_is_available(
            candidate,
            available_pools=available_pools,
            is_available=is_available,
            blocked=blocked,
            allow_blocked=allow_blocked,
            ignore_pool_availability=ignore_pool_availability,
        ):
            if advance:
                setattr(runtime, cursor_name, (index + 1) % len(candidates))
            return candidate, index
    return None


def _routing_weights(config: ModelPoolConfig) -> tuple[int, int]:
    stable_ratio = config.stable_ratio
    degraded_ratio = config.degraded_ratio
    divisor = gcd(stable_ratio, degraded_ratio)
    if divisor <= 0:
        return DEFAULT_STABLE_RATIO, DEFAULT_DEGRADED_RATIO
    return stable_ratio // divisor, degraded_ratio // divisor


def _record_selection(
    runtime: _AliasRuntime,
    *,
    model_name: str,
    pool_name: str,
    advance: bool,
) -> None:
    if not advance:
        return
    selected_at = time()
    runtime.selection_count += 1
    if pool_name == "degraded":
        runtime.degraded_requests += 1
    else:
        runtime.stable_requests += 1
    runtime.model_requests[model_name] = runtime.model_requests.get(model_name, 0) + 1
    runtime.model_last_used_at[model_name] = selected_at
    runtime.recent_routes.append(
        {
            "sequence": runtime.selection_count,
            "model": model_name,
            "pool": pool_name,
            "at": selected_at,
        }
    )
    if len(runtime.recent_routes) > 30:
        del runtime.recent_routes[:-30]


def _record_pool_event(
    runtime: _AliasRuntime,
    *,
    model_name: str,
    action: str,
    from_pool: str,
    to_pool: str,
) -> None:
    runtime.pool_event_count += 1
    runtime.pool_events.append(
        {
            "sequence": runtime.pool_event_count,
            "model": model_name,
            "action": action,
            "from_pool": from_pool,
            "to_pool": to_pool,
            "at": time(),
        }
    )
    if len(runtime.pool_events) > _MAX_POOL_EVENTS:
        del runtime.pool_events[:-_MAX_POOL_EVENTS]


def _select_virtual_candidate(
    alias_name: str,
    config: ModelPoolConfig,
    *,
    available_pools: frozenset[str] | None,
    is_available,
    blocked: frozenset[str],
    advance: bool,
) -> tuple[str, ModelSpec, tuple[str, ...], str] | None:
    with _ROUTING_LOCK:
        runtime = _runtime_for(alias_name, config)
        stable, degraded = _effective_pools(config, runtime)
        stable_weight, degraded_weight = _routing_weights(config)
        total_weight = stable_weight + degraded_weight
        slot = runtime.dispatch_cursor % total_weight if total_weight else 0
        preferred_pool = (
            "stable"
            if slot < stable_weight or not degraded
            else "degraded"
        )
        if advance:
            runtime.dispatch_cursor = (runtime.dispatch_cursor + 1) % max(total_weight, 1)

        pools = (
            ("stable", stable, runtime.stable_cursor, "stable_cursor"),
            ("degraded", degraded, runtime.degraded_cursor, "degraded_cursor"),
        )
        ordered_pools = sorted(
            pools,
            key=lambda item: 0 if item[0] == preferred_pool else 1,
        )

        selected: tuple[str, str] | None = None
        for pool_name, candidates, cursor, cursor_name in ordered_pools:
            picked = _pick_from_pool(
                candidates,
                cursor=cursor,
                runtime=runtime,
                cursor_name=cursor_name,
                available_pools=available_pools,
                is_available=is_available,
                blocked=blocked,
                allow_blocked=False,
                advance=advance,
            )
            if picked is not None:
                selected = picked[0], pool_name
                break

        if selected is None:
            # Preserve the previous behavior when every model is cooling down or
            # the test/runtime account snapshot is unavailable: return an enabled
            # candidate and let the downstream admission/fallback path decide.
            for pool_name, candidates, cursor, cursor_name in ordered_pools:
                picked = _pick_from_pool(
                    candidates,
                    cursor=cursor,
                    runtime=runtime,
                    cursor_name=cursor_name,
                    available_pools=available_pools,
                    is_available=is_available,
                    blocked=blocked,
                    allow_blocked=True,
                    ignore_pool_availability=True,
                    advance=advance,
                )
                if picked is not None:
                    selected = picked[0], pool_name
                    break

        if selected is None:
            return None

        candidate, pool_name = selected
        spec = registry.get(candidate)
        if spec is None or not spec.enabled:
            return None
        _record_selection(
            runtime,
            model_name=candidate,
            pool_name=pool_name,
            advance=advance,
        )
        return candidate, spec, (*stable, *degraded), pool_name


def fallback_candidates(resolution: ModelResolution) -> tuple[str, ...]:
    """Return other compatible candidates in effective pool priority order."""

    if not resolution.is_virtual:
        return ()

    result: list[str] = []
    seen: set[str] = {resolution.model}
    alias_name = getattr(resolution, "requested_model", "")
    for candidate in resolution.candidates:
        if candidate in seen:
            continue
        spec = registry.get(candidate)
        if (
            spec is not None
            and _candidate_is_usable(alias_name, candidate)
            and spec.capability == resolution.spec.capability
        ):
            result.append(candidate)
            seen.add(candidate)
    return tuple(result)


def resolve(
    model_name: str,
    *,
    available_pools: frozenset[str] | None = None,
    is_available=None,
    blocked_model_names: frozenset[str] | None = None,
    advance: bool = True,
) -> ModelResolution | None:
    """Resolve a client model name using weighted pool and round-robin routing.

    Structured aliases use a deterministic 95/5 weighted schedule by default.
    The selected pool then round-robins its eligible models. Legacy ordered
    arrays are treated as stable candidates and remain fully supported.
    """

    configs = alias_configs()
    config = configs.get(model_name)
    if config is not None:
        if blocked_model_names is None:
            from .cooldown import blocked_models

            blocked = blocked_models()
        else:
            blocked = blocked_model_names
        selected = _select_virtual_candidate(
            model_name,
            config,
            available_pools=available_pools,
            is_available=is_available,
            blocked=blocked,
            advance=advance,
        )
        if selected is None:
            return None
        candidate, spec, candidates, pool_name = selected
        return ModelResolution(
            model_name,
            candidate,
            spec,
            True,
            candidates,
            pool_name,
        )

    spec = registry.get(model_name)
    if spec is None or not spec.enabled:
        return None
    return ModelResolution(model_name, model_name, spec, False, ())


def list_virtual_models(
    *,
    available_pools: frozenset[str] | None = None,
    is_available=None,
) -> list[ModelResolution]:
    """Return configured virtual models without consuming routing capacity."""

    items: list[ModelResolution] = []
    for virtual_model in alias_configs():
        resolved = resolve(
            virtual_model,
            available_pools=available_pools,
            is_available=is_available,
            advance=False,
        )
        if resolved is not None:
            items.append(resolved)
    return items


def promote_model(model_name: str) -> None:
    """Move a successfully probed degraded model into its stable pool."""

    name = str(model_name or "").strip()
    if not name:
        return
    configs = alias_configs()
    with _ROUTING_LOCK:
        for alias_name, config in configs.items():
            runtime = _runtime_for(alias_name, config)
            if name not in config.degraded and name not in runtime.demoted:
                continue
            was_demoted = name in runtime.demoted
            was_promoted = name in runtime.promoted
            if was_demoted:
                runtime.demoted.discard(name)
            if name in config.degraded and not was_promoted:
                runtime.promoted.add(name)
            if was_demoted or (name in config.degraded and not was_promoted):
                _record_pool_event(
                    runtime,
                    model_name=name,
                    action="promote",
                    from_pool="degraded",
                    to_pool="stable",
                )


def demote_model(model_name: str) -> None:
    """Move a rate-limited stable model into the degraded probe pool."""

    name = str(model_name or "").strip()
    if not name:
        return
    configs = alias_configs()
    with _ROUTING_LOCK:
        for alias_name, config in configs.items():
            if name not in config.stable and name not in config.degraded:
                continue
            runtime = _runtime_for(alias_name, config)
            was_promoted = name in runtime.promoted
            was_demoted = name in runtime.demoted
            if was_promoted:
                runtime.promoted.discard(name)
            if name in config.stable and not was_demoted:
                runtime.demoted.add(name)
            if was_promoted or (name in config.stable and not was_demoted):
                _record_pool_event(
                    runtime,
                    model_name=name,
                    action="demote",
                    from_pool="stable",
                    to_pool="degraded",
                )


def reset_runtime_state() -> None:
    with _ROUTING_LOCK:
        _RUNTIMES.clear()


def routing_snapshot() -> dict[str, Any]:
    """Return process-local routing counters for the admin monitor page."""

    configs = alias_configs()
    aliases: list[dict[str, Any]] = []
    with _ROUTING_LOCK:
        for alias_name, config in configs.items():
            runtime = _runtime_for(alias_name, config)
            stable, degraded = _effective_pools(config, runtime)
            total = runtime.selection_count
            ratio_total = config.stable_ratio + config.degraded_ratio
            stable_target = (
                round(config.stable_ratio * 100 / ratio_total, 1)
                if ratio_total
                else 0
            )
            degraded_target = (
                round(config.degraded_ratio * 100 / ratio_total, 1)
                if ratio_total
                else 0
            )
            model_pool = {
                model: "stable"
                for model in stable
            }
            model_pool.update({model: "degraded" for model in degraded})
            model_ids = (*stable, *degraded)
            models = []
            for model in model_ids:
                requests = runtime.model_requests.get(model, 0)
                models.append(
                    {
                        "id": model,
                        "pool": model_pool[model],
                        "requests": requests,
                        "share": round(requests * 100 / total, 1) if total else 0,
                        "last_used_at": runtime.model_last_used_at.get(model),
                    }
                )
            aliases.append(
                {
                    "name": alias_name,
                    "target": {
                        "stable": stable_target,
                        "degraded": degraded_target,
                    },
                    "configured": config.as_dict(),
                    "effective": {
                        "stable": list(stable),
                        "degraded": list(degraded),
                    },
                    "stats": {
                        "total": total,
                        "stable": runtime.stable_requests,
                        "degraded": runtime.degraded_requests,
                        "stable_share": round(
                            runtime.stable_requests * 100 / total, 1
                        )
                        if total
                        else 0,
                        "degraded_share": round(
                            runtime.degraded_requests * 100 / total, 1
                        )
                        if total
                        else 0,
                        "started_at": runtime.started_at,
                    },
                    "models": models,
                    "recent": list(reversed(runtime.recent_routes)),
                    "pool_events": list(reversed(runtime.pool_events)),
                }
            )
    return {"generated_at": time(), "aliases": aliases}


__all__ = [
    "DEFAULT_ALIAS_CONFIG",
    "DEFAULT_DEGRADED_RATIO",
    "DEFAULT_STABLE_RATIO",
    "ModelPoolConfig",
    "ModelResolution",
    "alias_supported_in_api",
    "alias_config_map",
    "alias_configs",
    "alias_map",
    "demote_model",
    "fallback_candidates",
    "is_virtual_model",
    "is_resolution_usable",
    "list_virtual_models",
    "normalize_alias_config",
    "promote_model",
    "reset_runtime_state",
    "resolve",
    "routing_snapshot",
]
