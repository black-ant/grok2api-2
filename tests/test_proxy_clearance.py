import asyncio
import importlib
import unittest
from unittest.mock import AsyncMock, patch

from app.control.proxy import ProxyDirectory
from app.control.proxy.models import (
    ClearanceBundle,
    ClearanceBundleState,
    ClearanceMode,
    ProxyFeedback,
    ProxyFeedbackKind,
)


flaresolverr_module = importlib.import_module(
    "app.control.proxy.providers.flaresolverr"
)


class _FakeFlareSolverr:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def refresh_bundle(self, **kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return ClearanceBundle(
            bundle_id="flaresolverr:test@grok.com",
            cf_cookies="cf_clearance=refreshed",
            user_agent="Mozilla/5.0",
            affinity_key=kwargs["affinity_key"],
            clearance_host="grok.com",
        )


class _FakeConfig:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def get_str(self, key: str, default: str = "") -> str:
        if key == "proxy.clearance.mode":
            return self.mode
        if key == "proxy.clearance.flaresolverr_url":
            return "http://flaresolverr:8191"
        return default

    def get_int(self, _key: str, default: int = 0) -> int:
        return default


class ProxyClearanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_demand_waits_for_challenge_before_solving(self):
        directory = ProxyDirectory()
        directory._clearance_mode = ClearanceMode.ON_DEMAND
        solver = _FakeFlareSolverr()
        directory._flare = solver

        first = await directory.acquire()
        self.assertEqual(first.cf_cookies, "")
        self.assertEqual(solver.calls, 0)

        await directory.feedback(
            first,
            ProxyFeedback(kind=ProxyFeedbackKind.CHALLENGE, status_code=403),
        )
        bundle = directory.bundles[("direct", "grok.com")]
        self.assertEqual(bundle.state, ClearanceBundleState.INVALID)

        pending_first = asyncio.create_task(directory.acquire())
        pending_second = asyncio.create_task(directory.acquire())
        await solver.started.wait()
        self.assertEqual(solver.calls, 1)
        solver.release.set()
        refreshed_first, refreshed_second = await asyncio.gather(
            pending_first, pending_second
        )

        self.assertEqual(refreshed_first.cf_cookies, "cf_clearance=refreshed")
        self.assertEqual(refreshed_second.cf_cookies, "cf_clearance=refreshed")
        self.assertEqual(solver.calls, 1)
        self.assertEqual(
            (await directory.acquire()).cf_cookies,
            "cf_clearance=refreshed",
        )

    async def test_on_demand_does_not_run_scheduler_refresh(self):
        directory = ProxyDirectory()
        directory._clearance_mode = ClearanceMode.ON_DEMAND
        solver = _FakeFlareSolverr()
        directory._flare = solver

        await directory.warm_up()
        await directory.refresh_clearance_safe()

        self.assertEqual(solver.calls, 0)

    async def test_flaresolverr_provider_accepts_on_demand_mode(self):
        provider = flaresolverr_module.FlareSolverrClearanceProvider()
        with (
            patch.object(
                flaresolverr_module,
                "get_config",
                return_value=_FakeConfig("on_demand"),
            ),
            patch.object(
                provider,
                "_solve",
                new=AsyncMock(
                    return_value={
                        "cookies": "cf_clearance=ok",
                        "user_agent": "Mozilla/5.0",
                        "clearance_host": "grok.com",
                    }
                ),
            ) as solve,
        ):
            bundle = await provider.refresh_bundle(
                affinity_key="direct",
                proxy_url="",
                target_url="https://grok.com",
            )

        self.assertIsNotNone(bundle)
        self.assertEqual(bundle.cf_cookies, "cf_clearance=ok")
        solve.assert_awaited_once()

    def test_clearance_mode_parses_on_demand(self):
        self.assertEqual(ClearanceMode.parse("on_demand"), ClearanceMode.ON_DEMAND)


if __name__ == "__main__":
    unittest.main()
