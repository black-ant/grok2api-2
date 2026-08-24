"""Shared route decisions, attempts, fallback transitions, and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.control.model.aliases import (
    ModelResolution,
    fallback_candidate_pools,
    fallback_candidates,
)
from app.dataplane.shared.enums import POOL_ID_TO_STR
from app.products._model_fallback import fallback_limit, next_fallback_candidate


def _mask_key(token: str) -> str:
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


def _pool_name(pool_id: int | str | None) -> str | None:
    if pool_id is None:
        return None
    if isinstance(pool_id, int):
        return POOL_ID_TO_STR.get(pool_id, "basic")
    return str(pool_id)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    requested_model: str
    resolved_model: str
    route: str
    model_pool: str = "stable"
    is_virtual: bool = False
    fallback_candidates: tuple[str, ...] = ()
    fallback_pools: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_resolution(
        cls,
        resolution: ModelResolution,
        *,
        route: str | None = None,
    ) -> "RoutingDecision":
        candidates = fallback_candidates(resolution)
        pools = fallback_candidate_pools(resolution)
        return cls(
            requested_model=resolution.requested_model,
            resolved_model=resolution.model,
            route=route or ("console" if resolution.spec.is_console_chat() else "grok"),
            model_pool=resolution.pool,
            is_virtual=resolution.is_virtual,
            fallback_candidates=candidates,
            fallback_pools=tuple((model, pools[model]) for model in candidates if model in pools),
        )

    @classmethod
    def from_model(
        cls,
        model: str,
        *,
        candidates: tuple[str, ...] = (),
        routing: dict[str, Any] | None = None,
    ) -> "RoutingDecision":
        current = routing if isinstance(routing, dict) else {}
        route = str(current.get("route") or ("console" if model.endswith("-console") else "grok"))
        pools = current.get("fallback_candidate_pools")
        fallback_pools = (
            tuple((candidate, str(pools[candidate])) for candidate in candidates if candidate in pools)
            if isinstance(pools, dict)
            else ()
        )
        return cls(
            requested_model=str(current.get("virtual_model") or current.get("model") or model),
            resolved_model=str(current.get("resolved_model") or model),
            route=route,
            model_pool=str(current.get("model_pool") or "stable"),
            is_virtual=bool(current.get("virtual_model")),
            fallback_candidates=tuple(candidates),
            fallback_pools=fallback_pools,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "route": self.route,
            "model_pool": self.model_pool,
            "is_virtual": self.is_virtual,
            "fallback_candidates": list(self.fallback_candidates),
            "fallback_pools": dict(self.fallback_pools),
        }


@dataclass(slots=True)
class RouteAttempt:
    sequence: int
    model: str
    route: str
    model_pool: str
    account_pool: str | None = None
    mode_id: int | None = None
    routed_key_tail: str | None = None
    outcome: str = "pending"
    reason: str | None = None
    upstream_status: int | None = None
    downstream_status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "model": self.model,
            "route": self.route,
            "model_pool": self.model_pool,
            "account_pool": self.account_pool,
            "mode_id": self.mode_id,
            "routed_key_tail": self.routed_key_tail,
            "outcome": self.outcome,
            "reason": self.reason,
            "upstream_status": self.upstream_status,
            "downstream_status": self.downstream_status,
        }


class RoutingSession:
    """Track one request without moving account selection into this service."""

    def __init__(
        self,
        decision: RoutingDecision,
        *,
        routing: dict[str, Any] | None = None,
        fallback_budget: int = 0,
    ) -> None:
        self.decision = decision
        self.routing = routing if isinstance(routing, dict) else {}
        self.fallback_budget = max(0, int(fallback_budget))
        self._fallback_index = 0
        self._attempts: list[RouteAttempt] = []
        self._active: RouteAttempt | None = None
        self._decisions: list[dict[str, Any]] = [decision.as_dict()]
        self._original_model = decision.resolved_model
        self._original_pool = decision.model_pool
        self._sync()

    @classmethod
    def from_resolution(
        cls,
        resolution: ModelResolution,
        *,
        routing: dict[str, Any] | None = None,
        fallback_budget: int = 0,
        route: str | None = None,
    ) -> "RoutingSession":
        return cls(
            RoutingDecision.from_resolution(resolution, route=route),
            routing=routing,
            fallback_budget=fallback_budget,
        )

    @classmethod
    def from_model(
        cls,
        model: str,
        *,
        candidates: tuple[str, ...] = (),
        routing: dict[str, Any] | None = None,
        fallback_budget: int = 0,
    ) -> "RoutingSession":
        return cls(
            RoutingDecision.from_model(model, candidates=candidates, routing=routing),
            routing=routing,
            fallback_budget=fallback_budget,
        )

    @property
    def current_model(self) -> str:
        return self.decision.resolved_model

    def set_fallback_budget(self, budget: int) -> None:
        self.fallback_budget = max(0, int(budget))

    def _pool_for(self, model: str) -> str:
        if model == self._original_model:
            return self._original_pool
        return dict(self.decision.fallback_pools).get(model, "stable")

    def _sync(self) -> None:
        self.routing["route"] = self.decision.route
        self.routing["model_pool"] = self._pool_for(self.current_model)
        self.routing["fallback_candidate_pools"] = dict(self.decision.fallback_pools)
        self.routing["route_decisions"] = list(self._decisions)
        self.routing["route_attempts"] = [attempt.as_dict() for attempt in self._attempts]
        self.routing["fallback_count"] = len(self.routing.get("model_fallbacks") or [])
        if self.decision.is_virtual:
            self.routing["virtual_model"] = self.decision.requested_model
            self.routing["resolved_model"] = self.current_model
        else:
            self.routing["model"] = self.current_model

    def begin_attempt(
        self,
        model: str,
        *,
        token: str | None = None,
        mode_id: int | None = None,
        pool_id: int | str | None = None,
    ) -> RouteAttempt:
        if self._active is not None:
            self.complete_current(outcome="failed", reason="attempt_replaced")
        attempt = RouteAttempt(
            sequence=len(self._attempts) + 1,
            model=model,
            route=self.decision.route,
            model_pool=self._pool_for(model),
            account_pool=_pool_name(pool_id),
            mode_id=mode_id,
            routed_key_tail=token[-5:] if token else None,
        )
        self._attempts.append(attempt)
        self._active = attempt
        if token:
            self.routing["routed_key"] = _mask_key(token)
            self.routing["routed_key_tail"] = token[-5:]
        if mode_id is not None:
            self.routing["mode_id"] = mode_id
        if pool_id is not None:
            self.routing["pool"] = _pool_name(pool_id)
        self._sync()
        return attempt

    def complete_current(
        self,
        *,
        outcome: str,
        reason: str | None = None,
        upstream_status: int | None = None,
        downstream_status: int | None = None,
    ) -> None:
        if self._active is None:
            return
        self._active.outcome = outcome
        self._active.reason = reason
        self._active.upstream_status = upstream_status
        self._active.downstream_status = downstream_status
        self._active = None
        self._sync()

    def record_success(self, *, status: int = 200) -> None:
        self.complete_current(outcome="success", upstream_status=status)

    def record_failure(self, *, status: int | None = None, reason: str | None = None) -> None:
        self.complete_current(outcome="failed", reason=reason, upstream_status=status)

    def record_account_retry(self, *, status: int | None = None, reason: str = "upstream_retry") -> None:
        self.complete_current(outcome="account_retry", reason=reason, upstream_status=status)

    def next_model_fallback(
        self,
        *,
        status: int = 429,
        reason: str = "upstream_rate_limit",
        stream_started: bool = False,
    ) -> str | None:
        if stream_started:
            return None
        fallback = next_fallback_candidate(
            self.decision.fallback_candidates,
            self._fallback_index,
            self.fallback_budget,
        )
        if fallback is None:
            return None
        fallback_index, fallback_model = fallback
        previous_model = self.current_model
        self._fallback_index = fallback_index + 1
        self.complete_current(outcome="model_fallback", reason=reason, upstream_status=status)
        history = self.routing.setdefault("model_fallbacks", [])
        if isinstance(history, list):
            history.append({"from": previous_model, "to": fallback_model, "status": status})
        self.decision = RoutingDecision(
            requested_model=self.decision.requested_model,
            resolved_model=fallback_model,
            route=self.decision.route,
            model_pool=self._pool_for(fallback_model),
            is_virtual=self.decision.is_virtual,
            fallback_candidates=self.decision.fallback_candidates,
            fallback_pools=self.decision.fallback_pools,
        )
        self._decisions.append({**self.decision.as_dict(), "reason": reason, "upstream_status": status})
        self._sync()
        return fallback_model

    def record_downstream_status(self, status: int) -> None:
        self.routing["downstream_status"] = status
        self.routing["downstream_outcome"] = "success" if 200 <= status < 300 else "error"
        if self._attempts:
            self._attempts[-1].downstream_status = status
        self._sync()


def fallback_budget(config, candidates: tuple[str, ...], *, force_token: str | None = None) -> int:
    return fallback_limit(config, candidates, force_token=force_token)


__all__ = ["RouteAttempt", "RoutingDecision", "RoutingSession", "fallback_budget"]
