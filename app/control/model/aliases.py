"""Virtual model aliases backed by runtime config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.platform.config.snapshot import get_config

from . import registry
from .spec import ModelSpec


@dataclass(frozen=True)
class ModelResolution:
    """Resolved model request.

    ``requested_model`` is the client-facing value. ``model`` and ``spec`` are
    the real model selected for downstream routing.
    """

    requested_model: str
    model: str
    spec: ModelSpec
    is_virtual: bool
    candidates: tuple[str, ...] = ()


def _as_model_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def alias_map() -> dict[str, list[str]]:
    """Return configured virtual model aliases.

    Invalid value types are ignored. Model names are kept case-sensitive because
    the public contract requires exact virtual model names such as ``FREE`` and
    ``SUPER``.
    """

    raw = get_config("models.aliases", {})
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, list[str]] = {}
    for virtual_model, real_models in raw.items():
        name = str(virtual_model).strip()
        if not name:
            continue
        aliases[name] = _as_model_list(real_models)
    return aliases


def is_virtual_model(model_name: str) -> bool:
    return model_name in alias_map()


def _first_enabled(
    candidates: Iterable[str],
    blocked_model_names: frozenset[str] | None = None,
) -> tuple[str, ModelSpec] | None:
    blocked = blocked_model_names or frozenset()
    deferred: tuple[str, ModelSpec] | None = None
    for candidate in candidates:
        spec = registry.get(candidate)
        if spec is None or not spec.enabled:
            continue
        if candidate in blocked:
            if deferred is None:
                deferred = candidate, spec
            continue
        return candidate, spec
    return deferred


def fallback_candidates(resolution: ModelResolution) -> tuple[str, ...]:
    """Return lower-priority candidates compatible with the selected model."""
    if not resolution.is_virtual:
        return ()
    try:
        start = resolution.candidates.index(resolution.model) + 1
    except ValueError:
        return ()

    result: list[str] = []
    seen: set[str] = {resolution.model}
    for candidate in resolution.candidates[start:]:
        if candidate in seen:
            continue
        spec = registry.get(candidate)
        if (
            spec is not None
            and spec.enabled
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
) -> ModelResolution | None:
    """Resolve a client model name to a real registered model.

    For virtual models, candidates are tried in configured order. When
    ``available_pools`` and ``is_available`` are provided, the first currently
    routable candidate wins. Temporarily blocked candidates are skipped while
    a non-blocked candidate is available.
    """

    aliases = alias_map()
    candidates = tuple(aliases.get(model_name) or ())
    if candidates:
        blocked = blocked_model_names or frozenset()
        deferred: tuple[str, ModelSpec] | None = None
        if available_pools is not None and is_available is not None:
            for candidate in candidates:
                spec = registry.get(candidate)
                if spec is None or not is_available(spec, available_pools):
                    continue
                if candidate in blocked:
                    if deferred is None:
                        deferred = candidate, spec
                    continue
                return ModelResolution(model_name, candidate, spec, True, candidates)
            if deferred is not None:
                candidate, spec = deferred
                return ModelResolution(model_name, candidate, spec, True, candidates)
        selected = _first_enabled(candidates, blocked)
        if selected is None:
            return None
        candidate, spec = selected
        return ModelResolution(model_name, candidate, spec, True, candidates)

    spec = registry.get(model_name)
    if spec is None or not spec.enabled:
        return None
    return ModelResolution(model_name, model_name, spec, False, ())


def list_virtual_models(
    *,
    available_pools: frozenset[str] | None = None,
    is_available=None,
) -> list[ModelResolution]:
    """Return configured virtual models that resolve to a real model."""

    items: list[ModelResolution] = []
    for virtual_model in alias_map():
        resolved = resolve(
            virtual_model,
            available_pools=available_pools,
            is_available=is_available,
        )
        if resolved is not None:
            items.append(resolved)
    return items


__all__ = [
    "ModelResolution",
    "alias_map",
    "fallback_candidates",
    "is_virtual_model",
    "list_virtual_models",
    "resolve",
]
