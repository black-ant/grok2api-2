"""ProxyDirectory — control-plane proxy pool coordinator.

Maintains the list of EgressNodes and ClearanceBundles.
Selection delegates to the dataplane ProxyTable; this module owns
configuration loading and clearance refresh lifecycle.
"""

import asyncio
from urllib.parse import urlparse

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.runtime.clock import now_ms
from app.platform.runtime.ids import next_hex
from .config import resolve_clearance_config
from .models import (
    EgressMode,
    ClearanceMode,
    ClearanceBundleState,
    EgressNode,
    ClearanceBundle,
    ProxyLease,
    ProxyFeedback,
    ProxyFeedbackKind,
    RequestKind,
    ProxyScope,
)
from .providers.manual import ManualClearanceProvider
from .providers.flaresolverr import FlareSolverrClearanceProvider
from .bridge import KernelBridgeError, get_proxy_bridge_manager
from .clash import ClashParseError, find_clash_candidate, parse_clash_yaml

_DEFAULT_CLEARANCE_ORIGIN = "https://grok.com"
BundleKey = tuple[str, str]


def _clash_mapping(cfg, key: str) -> dict[str, str]:
    getter = getattr(cfg, "get", None)
    value = getter(key, {}) if getter else {}
    if not isinstance(value, dict):
        return {}
    return {
        str(proxy_id): str(kernel).strip()
        for proxy_id, kernel in value.items()
        if str(proxy_id).strip() and str(kernel).strip()
    }


def _unique_proxy_ids(values) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or []:
        proxy_id = str(value).strip()
        if proxy_id and proxy_id not in result:
            result.append(proxy_id)
    return tuple(result)


def _clearance_host(clearance_origin: str | None) -> str:
    host = urlparse(clearance_origin or _DEFAULT_CLEARANCE_ORIGIN).hostname
    return (host or "grok.com").lower()


class ProxyDirectory:
    """Owns egress nodes and clearance bundles.

    Thread-safety: all mutations are protected by ``_lock``.
    """

    def __init__(self) -> None:
        self._nodes: list[EgressNode] = []
        self._resource_nodes: list[EgressNode] = []  # for media downloads
        self._bundles: dict[BundleKey, ClearanceBundle] = {}
        self._lock = asyncio.Lock()
        # Single-flight guard: at most one clearance provider call per
        # proxy+host key. Other coroutines await the same task result.
        self._refresh_tasks: dict[BundleKey, asyncio.Task[ClearanceBundle | None]] = {}
        self._manual = ManualClearanceProvider()
        self._flare = FlareSolverrClearanceProvider()
        self._egress_mode: EgressMode = EgressMode.DIRECT
        self._clearance_mode: ClearanceMode = ClearanceMode.NONE
        self._config_sig: tuple | None = None
        # Pool cursor for PROXY_POOL mode: sticky routing with failure-driven rotate.
        # Incremented on node failure; all callers see the same cursor under _lock.
        self._pool_cursor: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Load proxy configuration from the current config snapshot."""
        cfg = get_config()
        egress_mode = EgressMode(cfg.get_str("proxy.egress.mode", "direct"))
        clearance_mode = ClearanceMode.parse(
            cfg.get_str("proxy.clearance.mode", "none")
        )
        base_url = cfg.get_str("proxy.egress.proxy_url", "")
        res_url = cfg.get_str("proxy.egress.resource_proxy_url", "")
        base_pool = tuple(cfg.get_list("proxy.egress.proxy_pool", []))
        res_pool = tuple(cfg.get_list("proxy.egress.resource_proxy_pool", []))
        if hasattr(cfg, 'get_bool'):
            clash_enabled = cfg.get_bool('proxy.clash.enabled', False)
        else:
            clash_enabled = cfg.get_str('proxy.clash.enabled', '').strip().lower() in {
                '1', 'true', 'yes', 'on'
            }
        clash_yaml = cfg.get_str('proxy.clash.yaml', '')
        clash_proxy_id = cfg.get_str('proxy.clash.selected_proxy_id', '')
        clash_kernel = cfg.get_str('proxy.clash.selected_kernel', 'auto')
        clash_url = cfg.get_str('proxy.clash.selected_url', '')
        clash_pool_ids = _unique_proxy_ids(
            cfg.get_list('proxy.clash.pool_proxy_ids', [])
        )
        clash_pool_kernels = _clash_mapping(cfg, 'proxy.clash.pool_kernels')
        resolved_pool_kernels: dict[str, str] = {}
        if clash_enabled:
            if clash_yaml and clash_pool_ids:
                try:
                    candidates = parse_clash_yaml(clash_yaml)
                    candidates_by_id = {
                        candidate.proxy_id: candidate for candidate in candidates
                    }
                    missing = [
                        proxy_id
                        for proxy_id in clash_pool_ids
                        if proxy_id not in candidates_by_id
                    ]
                    if missing:
                        raise ClashParseError('代理池节点不在当前 Clash YAML 中')
                    bridge_manager = get_proxy_bridge_manager()
                    pool_urls: list[str] = []
                    for proxy_id in clash_pool_ids:
                        candidate = candidates_by_id[proxy_id]
                        preferred_kernel = (
                            'native'
                            if candidate.kernels == ('native',)
                            else (
                                clash_pool_kernels.get(proxy_id)
                                or clash_kernel
                                or 'auto'
                            )
                        )
                        selected_url, selected_kernel = (
                            await bridge_manager.ensure_candidate(
                                candidate,
                                preferred_kernel,
                                auto_download=cfg.get_bool(
                                    'proxy.kernels.auto_download', False
                                ),
                            )
                        )
                        pool_urls.append(selected_url)
                        resolved_pool_kernels[proxy_id] = selected_kernel
                    egress_mode = EgressMode.PROXY_POOL
                    base_pool = tuple(pool_urls)
                    res_pool = tuple(pool_urls)
                    clash_url = pool_urls[0]
                    clash_kernel = resolved_pool_kernels[clash_pool_ids[0]]
                except (ClashParseError, KernelBridgeError, RuntimeError) as exc:
                    raise RuntimeError(f'Clash 代理池启动失败：{exc}') from exc
            elif clash_yaml and clash_proxy_id:
                try:
                    candidate = find_clash_candidate(clash_yaml, clash_proxy_id)
                    bridge_manager = get_proxy_bridge_manager()
                    clash_url, clash_kernel = await bridge_manager.ensure_candidate(
                        candidate,
                        clash_kernel,
                        auto_download=cfg.get_bool(
                            'proxy.kernels.auto_download', False
                        ),
                    )
                except (ClashParseError, KernelBridgeError, RuntimeError) as exc:
                    raise RuntimeError(f'Clash 全局代理启动失败：{exc}') from exc
            if clash_url and egress_mode != EgressMode.PROXY_POOL:
                egress_mode = EgressMode.SINGLE_PROXY
                base_url = clash_url
                res_url = clash_url
                base_pool = ()
                res_pool = ()

        clearance = resolve_clearance_config(cfg)
        config_sig = (
            clash_enabled,
            clash_yaml,
            clash_proxy_id,
            clash_kernel,
            clash_url,
            clash_pool_ids,
            tuple(sorted(clash_pool_kernels.items())),
            tuple(sorted(resolved_pool_kernels.items())),
            egress_mode.value,
            clearance_mode.value,
            base_url,
            res_url,
            base_pool,
            res_pool,
            cfg.get_str("proxy.clearance.flaresolverr_url", ""),
            clearance.cf_cookies,
            clearance.user_agent,
            clearance.cf_clearance,
            clearance.browser,
            cfg.get_int("proxy.clearance.timeout_sec", 60),
        )

        nodes: list[EgressNode] = []
        resource_nodes: list[EgressNode] = []

        if egress_mode == EgressMode.SINGLE_PROXY:
            if base_url:
                nodes.append(EgressNode(node_id="single", proxy_url=base_url))
            if res_url:
                resource_nodes.append(
                    EgressNode(node_id="res-single", proxy_url=res_url)
                )

        elif egress_mode == EgressMode.PROXY_POOL:
            for i, url in enumerate(base_pool):
                nodes.append(EgressNode(node_id=f"pool-{i}", proxy_url=url))
            for i, url in enumerate(res_pool):
                resource_nodes.append(
                    EgressNode(node_id=f"res-pool-{i}", proxy_url=url)
                )

        valid_affinities = {n.proxy_url or "direct" for n in [*nodes, *resource_nodes]}
        if not valid_affinities:
            valid_affinities = {"direct"}

        async with self._lock:
            if self._config_sig == config_sig:
                return
            self._egress_mode = egress_mode
            self._clearance_mode = clearance_mode
            self._nodes = nodes
            self._resource_nodes = resource_nodes
            self._pool_cursor = 0
            self._bundles = {
                key: bundle.model_copy(update={"state": ClearanceBundleState.INVALID})
                for key, bundle in self._bundles.items()
                if key[0] in valid_affinities
            }
            self._refresh_tasks = {
                key: task
                for key, task in self._refresh_tasks.items()
                if key[0] in valid_affinities and not task.done()
            }
            self._config_sig = config_sig

        logger.info(
            "proxy directory loaded: egress_mode={} clearance_mode={} node_count={} resource_node_count={}",
            egress_mode,
            clearance_mode,
            len(nodes),
            len(resource_nodes),
        )

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    async def acquire(
        self,
        *,
        scope: ProxyScope = ProxyScope.APP,
        kind: RequestKind = RequestKind.HTTP,
        resource: bool = False,
        clearance_origin: str | None = None,
    ) -> ProxyLease:
        """Return a ProxyLease for the next request.

        For DIRECT mode, returns a lease with no proxy or clearance.
        """
        proxy_url = await self._pick_proxy_url(resource=resource)
        affinity = proxy_url or "direct"
        clearance_host = _clearance_host(clearance_origin)

        bundle = await self._get_or_build_bundle(
            affinity_key=affinity,
            proxy_url=proxy_url or "",
            clearance_origin=clearance_origin or _DEFAULT_CLEARANCE_ORIGIN,
        )

        return ProxyLease(
            lease_id=next_hex(),
            proxy_url=proxy_url,
            cf_cookies=bundle.cf_cookies if bundle else "",
            user_agent=bundle.user_agent if bundle else "",
            clearance_host=clearance_host,
            scope=scope,
            kind=kind,
            acquired_at=now_ms(),
        )

    async def feedback(self, lease: ProxyLease, result: ProxyFeedback) -> None:
        """Apply upstream feedback to the appropriate egress node."""
        if result.kind in (
            ProxyFeedbackKind.CHALLENGE,
            ProxyFeedbackKind.UNAUTHORIZED,
        ):
            # Invalidate associated clearance bundle.
            key = (lease.proxy_url or "direct", lease.clearance_host)
            async with self._lock:
                bundle = self._bundles.get(key)
                if bundle:
                    self._bundles[key] = bundle.model_copy(
                        update={"state": ClearanceBundleState.INVALID}
                    )
                elif self._clearance_mode != ClearanceMode.NONE:
                    # Keep a per-affinity marker so on-demand mode knows that
                    # the next acquire must solve instead of returning empty
                    # clearance again.
                    self._bundles[key] = ClearanceBundle(
                        bundle_id=f"invalid:{next_hex()}",
                        state=ClearanceBundleState.INVALID,
                        affinity_key=key[0],
                        clearance_host=key[1],
                    )

        # In PROXY_POOL mode, rotate to the next node on any failure so the
        # next acquire() prefers a different egress rather than hammering the
        # same broken node.
        if (
            self._egress_mode == EgressMode.PROXY_POOL
            and lease.proxy_url
            and result.kind
            in (
                ProxyFeedbackKind.CHALLENGE,
                ProxyFeedbackKind.UNAUTHORIZED,
                ProxyFeedbackKind.FORBIDDEN,
                ProxyFeedbackKind.TRANSPORT_ERROR,
            )
        ):
            async with self._lock:
                self._pool_cursor += 1
                logger.debug(
                    "proxy pool cursor advanced: proxy={} kind={} cursor={}",
                    lease.proxy_url,
                    result.kind,
                    self._pool_cursor,
                )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _pick_proxy_url(self, resource: bool = False) -> str | None:
        if self._egress_mode == EgressMode.DIRECT:
            return None
        async with self._lock:
            # Prefer resource-specific nodes when available; fall back to base nodes.
            nodes = (
                self._resource_nodes
                if resource and self._resource_nodes
                else self._nodes
            )
            if not nodes:
                return None
            if self._egress_mode == EgressMode.SINGLE_PROXY:
                return nodes[0].proxy_url
            # PROXY_POOL: sticky routing — use current cursor, rotate on failure.
            idx = self._pool_cursor % len(nodes)
            return nodes[idx].proxy_url

    async def _get_or_build_bundle(
        self,
        *,
        affinity_key: str,
        proxy_url: str,
        clearance_origin: str,
    ) -> ClearanceBundle | None:
        if self._clearance_mode == ClearanceMode.NONE:
            return None
        clearance_host = _clearance_host(clearance_origin)
        key: BundleKey = (affinity_key, clearance_host)

        async with self._lock:
            bundle = self._bundles.get(key)
            if bundle and bundle.state == ClearanceBundleState.VALID:
                return bundle
            if self._clearance_mode == ClearanceMode.ON_DEMAND and bundle is None:
                # Do not block the first request on FlareSolverr. A challenge
                # feedback creates an INVALID marker, which enables the next
                # acquire to perform the first solve on demand.
                return None
            task = self._refresh_tasks.get(key)
            if task is None or task.done():
                task = asyncio.create_task(
                    self._refresh_bundle(
                        key=key,
                        affinity_key=affinity_key,
                        proxy_url=proxy_url,
                        clearance_host=clearance_host,
                        clearance_origin=clearance_origin,
                    ),
                    name=f"clearance-refresh:{affinity_key}:{clearance_host}",
                )
                self._refresh_tasks[key] = task

        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._refresh_tasks.get(key) is task:
                        self._refresh_tasks.pop(key, None)

    async def _refresh_bundle(
        self,
        *,
        key: BundleKey,
        affinity_key: str,
        proxy_url: str,
        clearance_host: str,
        clearance_origin: str,
    ) -> ClearanceBundle | None:
        if self._clearance_mode == ClearanceMode.MANUAL:
            bundle = self._manual.build_bundle(
                affinity_key=affinity_key,
                clearance_host=clearance_host,
            )
        elif self._clearance_mode in {
            ClearanceMode.FLARESOLVERR,
            ClearanceMode.ON_DEMAND,
        }:
            bundle = await self._flare.refresh_bundle(
                affinity_key=affinity_key,
                proxy_url=proxy_url,
                target_url=clearance_origin,
            )
        else:
            bundle = None

        if bundle:
            bundle = bundle.model_copy(update={"last_refresh_at": now_ms()})
            async with self._lock:
                self._bundles[key] = bundle
        return bundle

    # ------------------------------------------------------------------
    # Clearance lifecycle helpers (used by ProxyClearanceScheduler)
    # ------------------------------------------------------------------

    async def invalidate_clearance(self) -> None:
        """Mark all cached clearance bundles as invalid.

        The next ``acquire()`` call for each affinity key will trigger a fresh
        FlareSolverr fetch (serialised by the single-flight guard).
        """
        async with self._lock:
            self._bundles = {
                k: b.model_copy(update={"state": ClearanceBundleState.INVALID})
                for k, b in self._bundles.items()
            }
        logger.debug("clearance bundles invalidated: count={}", len(self._bundles))

    async def warm_up(self) -> None:
        """Pre-fetch clearance bundles for all configured affinity keys.

        Called once at startup so the first real request does not have to wait
        for FlareSolverr.  Does NOT invalidate existing bundles first.
        """
        if self._clearance_mode in {
            ClearanceMode.NONE,
            ClearanceMode.ON_DEMAND,
        }:
            return
        async with self._lock:
            nodes = list(self._nodes)
        affinity_keys = (
            [(n.proxy_url or "direct", n.proxy_url or "") for n in nodes]
            if nodes
            else [("direct", "")]
        )
        for affinity, proxy_url in affinity_keys:
            await self._get_or_build_bundle(
                affinity_key=affinity,
                proxy_url=proxy_url,
                clearance_origin=_DEFAULT_CLEARANCE_ORIGIN,
            )

    async def refresh_clearance_safe(self) -> None:
        """Scheduled clearance refresh: build new bundles then swap atomically.

        Unlike ``invalidate_clearance() + warm_up()``, this never discards a
        working bundle before a replacement is ready.  If FlareSolverr is
        temporarily unavailable the old bundle remains valid and continues to
        serve requests.
        """
        if self._clearance_mode != ClearanceMode.FLARESOLVERR:
            return
        async with self._lock:
            nodes = list(self._nodes)
            existing = list(self._bundles.keys())

        refresh_targets: dict[BundleKey, tuple[str, str]] = {}
        default_items = (
            [(n.proxy_url or "direct", n.proxy_url or "") for n in nodes]
            if nodes
            else [("direct", "")]
        )
        for affinity, proxy_url in default_items:
            key: BundleKey = (affinity, _clearance_host(_DEFAULT_CLEARANCE_ORIGIN))
            refresh_targets[key] = (proxy_url, _DEFAULT_CLEARANCE_ORIGIN)
        for key in existing:
            affinity, clearance_host = key
            refresh_targets.setdefault(
                key,
                ("" if affinity == "direct" else affinity, f"https://{clearance_host}"),
            )

        for key, (proxy_url, clearance_origin) in refresh_targets.items():
            affinity, clearance_host = key
            if self._clearance_mode == ClearanceMode.MANUAL:
                new_bundle = self._manual.build_bundle(
                    affinity_key=affinity,
                    clearance_host=clearance_host,
                )
            else:
                new_bundle = await self._flare.refresh_bundle(
                    affinity_key=affinity,
                    proxy_url=proxy_url,
                    target_url=clearance_origin,
                )
            if new_bundle:
                async with self._lock:
                    self._bundles[key] = new_bundle
                logger.debug("clearance bundle refreshed: bundle={}", key)
            else:
                logger.warning(
                    "clearance refresh failed, keeping old bundle: bundle={}",
                    key,
                )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def egress_mode(self) -> EgressMode:
        return self._egress_mode

    @property
    def clearance_mode(self) -> ClearanceMode:
        return self._clearance_mode

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def nodes(self) -> list[EgressNode]:
        """Read-only snapshot of the current egress node list."""
        return list(self._nodes)

    @property
    def bundles(self) -> dict[BundleKey, ClearanceBundle]:
        """Read-only snapshot of the current clearance bundles."""
        return dict(self._bundles)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_directory: ProxyDirectory | None = None


async def get_proxy_directory() -> ProxyDirectory:
    """Return the module-level ProxyDirectory, reloading config if it changed."""
    global _directory
    if _directory is None:
        _directory = ProxyDirectory()
    await _directory.load()
    return _directory


__all__ = ["ProxyDirectory", "get_proxy_directory"]
