import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import orjson

from app.control.model import registry as model_registry
from app.platform.errors import UpstreamError
from app.products.web.admin.capability import CapabilityScanRequest, scan_capabilities


class _Repo:
    async def get_accounts(self, tokens):
        return []


class AdminCapabilityRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_routing_scan_uses_reserved_account_and_feedback(self):
        spec = model_registry.get("grok-4.20-0309-console")
        self.assertIsNotNone(spec)
        lease = SimpleNamespace(token="routing-token", pool_id=0)
        directory = SimpleNamespace(
            release=AsyncMock(),
            feedback=AsyncMock(),
        )

        with (
            patch(
                "app.products.web.admin.capability._scan_models",
                return_value=[spec],
            ),
            patch(
                "app.products.web.admin.capability.get_account_directory",
                new=AsyncMock(return_value=directory),
            ),
            patch(
                "app.products.web.admin.capability.reserve_account",
                new=AsyncMock(return_value=(lease, 5)),
            ) as reserve,
            patch(
                "app.products.web.admin.capability._probe_console_model",
                new=AsyncMock(return_value="ok"),
            ) as probe,
            patch(
                "app.products.web.admin.capability.get_config",
                return_value=SimpleNamespace(get_float=lambda key, default: default),
            ),
            patch(
                "app.products.web.admin.capability._scan_question",
                return_value="question",
            ),
            patch(
                "app.products.web.admin.capability._schedule_routing_outcome_sync",
            ) as schedule,
        ):
            response = await scan_capabilities(
                CapabilityScanRequest(
                    model=spec.model_name,
                    selection_mode="routing",
                ),
                repo=_Repo(),
            )

        body = orjson.loads(response.body)
        reserve.assert_awaited_once_with(directory, spec)
        probe.assert_awaited_once_with(
            "routing-token",
            spec.model_name,
            120.0,
            "question",
        )
        directory.release.assert_awaited_once_with(lease)
        directory.feedback.assert_awaited_once()
        schedule.assert_called_once_with("routing-token", 5, success=True, exc=None)
        self.assertEqual(body["selection_mode"], "routing")
        self.assertEqual(body["results"][0]["account_pool"], "basic")
        self.assertEqual(body["results"][0]["token_tail"], "oken")
        self.assertEqual(body["results"][0]["status"], "available")

    async def test_routing_scan_records_rate_limit_feedback(self):
        spec = model_registry.get("grok-4.20-0309-console")
        self.assertIsNotNone(spec)
        lease = SimpleNamespace(token="routing-token", pool_id=0)
        directory = SimpleNamespace(
            release=AsyncMock(),
            feedback=AsyncMock(),
        )
        failure = UpstreamError("rate limited", status=429)

        with (
            patch(
                "app.products.web.admin.capability._scan_models",
                return_value=[spec],
            ),
            patch(
                "app.products.web.admin.capability.get_account_directory",
                new=AsyncMock(return_value=directory),
            ),
            patch(
                "app.products.web.admin.capability.reserve_account",
                new=AsyncMock(return_value=(lease, 5)),
            ),
            patch(
                "app.products.web.admin.capability._probe_console_model",
                new=AsyncMock(side_effect=failure),
            ),
            patch(
                "app.products.web.admin.capability.get_config",
                return_value=SimpleNamespace(get_float=lambda key, default: default),
            ),
            patch(
                "app.products.web.admin.capability._scan_question",
                return_value="question",
            ),
            patch(
                "app.products.web.admin.capability._schedule_routing_outcome_sync",
            ) as schedule,
        ):
            response = await scan_capabilities(
                CapabilityScanRequest(
                    model=spec.model_name,
                    selection_mode="routing",
                ),
                repo=_Repo(),
            )

        body = orjson.loads(response.body)
        directory.release.assert_awaited_once_with(lease)
        directory.feedback.assert_awaited_once()
        feedback_args = directory.feedback.await_args.args
        self.assertEqual(feedback_args[0], "routing-token")
        self.assertEqual(feedback_args[1].value, "rate_limited")
        self.assertEqual(feedback_args[2], 5)
        schedule.assert_called_once_with("routing-token", 5, success=False, exc=failure)
        self.assertEqual(body["results"][0]["status"], "rate_limited")

    async def test_empty_probe_response_is_not_available(self):
        spec = model_registry.get("grok-4.20-0309-console")
        self.assertIsNotNone(spec)
        lease = SimpleNamespace(token="routing-token", pool_id=0)
        directory = SimpleNamespace(
            release=AsyncMock(),
            feedback=AsyncMock(),
        )

        with (
            patch("app.products.web.admin.capability._scan_models", return_value=[spec]),
            patch(
                "app.products.web.admin.capability.get_account_directory",
                new=AsyncMock(return_value=directory),
            ),
            patch(
                "app.products.web.admin.capability.reserve_account",
                new=AsyncMock(return_value=(lease, 5)),
            ),
            patch(
                "app.products.web.admin.capability._probe_console_model",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "app.products.web.admin.capability.get_config",
                return_value=SimpleNamespace(get_float=lambda key, default: default),
            ),
            patch(
                "app.products.web.admin.capability._scan_question",
                return_value="question",
            ),
            patch("app.products.web.admin.capability._schedule_routing_outcome_sync") as schedule,
        ):
            response = await scan_capabilities(
                CapabilityScanRequest(
                    model=spec.model_name,
                    selection_mode="routing",
                ),
                repo=_Repo(),
            )

        body = orjson.loads(response.body)
        self.assertEqual(body["results"][0]["status"], "unknown")
        self.assertEqual(body["results"][0]["message"], "模型未返回消息")
        directory.feedback.assert_awaited_once()
        schedule.assert_called_once_with("routing-token", 5, success=False, exc=unittest.mock.ANY)


if __name__ == "__main__":
    unittest.main()
