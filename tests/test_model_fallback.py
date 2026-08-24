import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.control.model import cooldown
from app.control.model.cooldown import (
    ModelAdmission,
    admit_model,
    blocked_models,
    clear_rate_limit,
    mark_model_success,
    mark_rate_limited,
    model_status_snapshot,
    release_probe,
    reset_rate_limits,
)
from app.products._model_fallback import (
    fallback_limit,
    max_cooldown_seconds,
    next_fallback_candidate,
    record_fallback,
)
from app.platform.errors import UpstreamError, parse_retry_after
from app.products.openai import console_chat


class _Config:
    def __init__(
        self,
        enabled=True,
        limit=5,
        cooldown=120,
        max_cooldown=1800,
        jitter=0.0,
        retry_codes="429",
    ):
        self.enabled = enabled
        self.limit = limit
        self.cooldown = cooldown
        self.max_cooldown = max_cooldown
        self.jitter = jitter
        self.retry_codes = retry_codes

    def get_bool(self, key, default=False):
        if key == "features.auto_model_fallback":
            return self.enabled
        return default

    def get_int(self, key, default=0):
        if key == "retry.model_fallback_max_retries":
            return self.limit
        if key == "retry.model_fallback_cooldown_sec":
            return self.cooldown
        if key == "retry.model_fallback_max_cooldown_sec":
            return self.max_cooldown
        return default

    def get(self, key, default=None):
        if key == "retry.on_codes":
            return self.retry_codes
        return default

    def get_float(self, key, default=0.0):
        if key == "retry.model_fallback_jitter_ratio":
            return self.jitter
        return default


class ModelFallbackTests(unittest.TestCase):
    def tearDown(self):
        reset_rate_limits()

    def test_fallback_limit_is_bounded_by_candidates(self):
        self.assertEqual(
            fallback_limit(_Config(limit=5), ("model-1", "model-2")),
            2,
        )

    def test_fallback_limit_can_be_disabled(self):
        self.assertEqual(
            fallback_limit(_Config(enabled=False), ("model-1",)),
            0,
        )

    def test_rate_limited_model_is_temporarily_blocked(self):
        mark_rate_limited("model-1", 60)
        self.assertIn("model-1", blocked_models())
        clear_rate_limit("model-1")
        self.assertNotIn("model-1", blocked_models())

    def test_rate_limit_uses_backoff_and_single_recovery_probe(self):
        clock = [100.0]
        with patch.object(cooldown, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(
                mark_rate_limited("model-1", 10, max_cooldown_sec=40),
                10,
            )
            self.assertEqual(admit_model("model-1"), ModelAdmission.BLOCKED)

            clock[0] = 110.0
            self.assertEqual(admit_model("model-1"), ModelAdmission.PROBE)
            self.assertEqual(admit_model("model-1"), ModelAdmission.BLOCKED)
            self.assertEqual(
                mark_rate_limited("model-1", 10, max_cooldown_sec=40),
                20,
            )

            clock[0] = 130.0
            self.assertEqual(
                mark_rate_limited("model-1", 10, max_cooldown_sec=40),
                40,
            )
            status = model_status_snapshot()["model-1"]
            self.assertEqual(status["consecutive_rate_limits"], 3)
            self.assertEqual(status["last_delay_sec"], 40)
            self.assertEqual(status["status"], "cooling")

            clock[0] = 169.0
            self.assertEqual(admit_model("model-1"), ModelAdmission.BLOCKED)
            clock[0] = 170.0
            self.assertEqual(admit_model("model-1"), ModelAdmission.PROBE)
            mark_model_success("model-1")
            self.assertEqual(admit_model("model-1"), ModelAdmission.NORMAL)

    def test_failed_probe_can_be_released(self):
        clock = [100.0]
        with patch.object(cooldown, "monotonic", side_effect=lambda: clock[0]):
            mark_rate_limited("model-1", 10)
            clock[0] = 110.0
            self.assertEqual(admit_model("model-1"), ModelAdmission.PROBE)
            release_probe("model-1")
            self.assertEqual(admit_model("model-1"), ModelAdmission.PROBE)
            mark_model_success("model-1")

    def test_fallback_skips_a_cooling_candidate(self):
        mark_rate_limited("model-2", 60)
        self.assertEqual(
            next_fallback_candidate(("model-2", "model-3"), 0, 2),
            (1, "model-3"),
        )

    def test_max_cooldown_is_not_below_base_cooldown(self):
        self.assertEqual(max_cooldown_seconds(_Config(cooldown=120)), 1800)
        self.assertEqual(max_cooldown_seconds(_Config(cooldown=2000)), 2000)

    def test_retry_after_seconds_are_parsed(self):
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertEqual(parse_retry_after(b"0"), 0.0)

    def test_retry_after_http_date_is_parsed(self):
        now_s = 1_700_000_000.0
        retry_at = datetime.fromtimestamp(now_s + 90, tz=timezone.utc)
        header = format_datetime(retry_at, usegmt=True)
        self.assertAlmostEqual(parse_retry_after(header, now_s=now_s), 90.0)

    def test_upstream_retry_after_overrides_local_maximum(self):
        clock = [100.0]
        with patch.object(cooldown, "monotonic", side_effect=lambda: clock[0]):
            delay = mark_rate_limited(
                "model-1",
                10,
                max_cooldown_sec=20,
                retry_after_sec=120,
            )
            self.assertEqual(delay, 120)
            clock[0] = 219.9
            self.assertIn("model-1", blocked_models())
            clock[0] = 220.0
            self.assertEqual(admit_model("model-1"), ModelAdmission.PROBE)

    def test_progressive_backoff_does_not_shorten_after_large_retry_after(self):
        clock = [100.0]
        with patch.object(cooldown, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(
                mark_rate_limited(
                    "model-1",
                    10,
                    max_cooldown_sec=40,
                    retry_after_sec=120,
                ),
                120,
            )
            self.assertEqual(
                mark_rate_limited(
                    "model-1",
                    10,
                    max_cooldown_sec=40,
                    retry_after_sec=5,
                ),
                120,
            )

    def test_jitter_is_randomized_but_stays_within_local_maximum(self):
        with patch.object(cooldown.random, "uniform", return_value=5.0) as uniform:
            delay = mark_rate_limited(
                "model-1",
                10,
                max_cooldown_sec=12,
                jitter_ratio=0.5,
            )
        self.assertEqual(delay, 12)
        uniform.assert_called_once_with(0.0, 5.0)

    def test_record_fallback_updates_routing_history(self):
        routing = {"resolved_model": "model-1"}

        record_fallback(
            routing,
            from_model="model-1",
            to_model="model-2",
            status=429,
        )

        self.assertEqual(routing["resolved_model"], "model-2")
        self.assertEqual(
            routing["model_fallbacks"],
            [{"from": "model-1", "to": "model-2", "status": 429}],
        )


class ConsoleModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        reset_rate_limits()

    async def test_console_stream_downgrades_model_after_429(self):
        first_model = "grok-4.20-0309-reasoning-console"
        fallback_model = "grok-4.20-0309-non-reasoning-console"
        first_payload_model = "grok-4.20-0309-reasoning"
        fallback_payload_model = "grok-4.20-0309-non-reasoning"
        accounts = iter([
            SimpleNamespace(token="token-1", pool_id=0),
            SimpleNamespace(token="token-2", pool_id=0),
        ])
        calls = []

        async def fake_stream(token, payload, timeout_s):
            calls.append((token, payload["model"]))
            if payload["model"] == first_payload_model:
                raise UpstreamError("rate limited", status=429)
            if False:
                yield "unused"

        async def fake_reserve(*_args, **_kwargs):
            return next(accounts), 5

        directory = SimpleNamespace(
            release=AsyncMock(),
            feedback=AsyncMock(),
        )
        routing = {"virtual_model": "FREE", "resolved_model": first_model}

        with patch.object(console_chat, "get_config", return_value=_Config(limit=1, retry_codes="")), patch.object(
            console_chat, "selection_max_retries", return_value=0
        ), patch.object(
            console_chat, "reserve_account", new=AsyncMock(side_effect=fake_reserve)
        ), patch.object(
            console_chat, "stream_console_chat", new=fake_stream
        ), patch(
            "app.dataplane.account._directory", directory
        ):
            result = await console_chat.completions(
                model=first_model,
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                model_fallbacks=(fallback_model,),
                request_log_routing=routing,
            )
            chunks = [chunk async for chunk in result]

        self.assertEqual(calls, [("token-1", first_payload_model), ("token-2", fallback_payload_model)])
        self.assertTrue(chunks)
        self.assertEqual(routing["resolved_model"], fallback_model)
        self.assertEqual(
            routing["model_fallbacks"],
            [{"from": first_model, "to": fallback_model, "status": 429}],
        )

    async def test_console_stream_does_not_switch_after_first_chunk(self):
        first_model = "grok-4.20-0309-reasoning-console"
        fallback_model = "grok-4.20-0309-non-reasoning-console"
        first_payload_model = "grok-4.20-0309-reasoning"
        accounts = iter([SimpleNamespace(token="token-1", pool_id=0)])
        calls = []

        async def fake_stream(token, payload, timeout_s):
            calls.append((token, payload["model"]))
            yield "response.output_text.delta", '{"delta":"partial"}'
            raise UpstreamError("rate limited", status=429)

        async def fake_reserve(*_args, **_kwargs):
            return next(accounts), 5

        directory = SimpleNamespace(
            release=AsyncMock(),
            feedback=AsyncMock(),
        )
        routing = {"virtual_model": "FREE", "resolved_model": first_model}

        with patch.object(console_chat, "get_config", return_value=_Config(limit=1)), patch.object(
            console_chat, "selection_max_retries", return_value=0
        ), patch.object(
            console_chat, "reserve_account", new=AsyncMock(side_effect=fake_reserve)
        ), patch.object(
            console_chat, "stream_console_chat", new=fake_stream
        ), patch(
            "app.dataplane.account._directory", directory
        ):
            result = await console_chat.completions(
                model=first_model,
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                model_fallbacks=(fallback_model,),
                request_log_routing=routing,
            )
            received = []
            with self.assertRaises(UpstreamError):
                async for chunk in result:
                    received.append(chunk)

        self.assertEqual(calls, [("token-1", first_payload_model)])
        self.assertEqual(len(received), 1)
        self.assertNotIn("model_fallbacks", routing)

    async def test_console_stream_recovers_model_after_cooldown(self):
        first_model = "grok-4.20-0309-reasoning-console"
        fallback_model = "grok-4.20-0309-non-reasoning-console"
        first_payload_model = "grok-4.20-0309-reasoning"
        fallback_payload_model = "grok-4.20-0309-non-reasoning"
        accounts = iter([
            SimpleNamespace(token="token-1", pool_id=0),
            SimpleNamespace(token="token-2", pool_id=0),
        ])
        calls = []

        async def fake_stream(token, payload, timeout_s):
            calls.append((token, payload["model"]))
            yield "response.output_text.delta", '{"delta":"ok"}'

        async def fake_reserve(*_args, **_kwargs):
            return next(accounts), 5

        directory = SimpleNamespace(
            release=AsyncMock(),
            feedback=AsyncMock(),
        )
        first_routing = {"virtual_model": "FREE", "resolved_model": first_model}
        recovered_routing = {"virtual_model": "FREE", "resolved_model": first_model}
        clock = [100.0]

        with patch.object(cooldown, "monotonic", side_effect=lambda: clock[0]), patch.object(
            console_chat, "get_config", return_value=_Config(limit=1, cooldown=10)
        ), patch.object(
            console_chat, "selection_max_retries", return_value=0
        ), patch.object(
            console_chat, "reserve_account", new=AsyncMock(side_effect=fake_reserve)
        ), patch.object(
            console_chat, "stream_console_chat", new=fake_stream
        ), patch(
            "app.dataplane.account._directory", directory
        ):
            mark_rate_limited(first_model, 10, max_cooldown_sec=10)
            first_result = await console_chat.completions(
                model=first_model,
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                model_fallbacks=(fallback_model,),
                request_log_routing=first_routing,
            )
            first_chunks = [chunk async for chunk in first_result]

            clock[0] = 110.0
            recovered_result = await console_chat.completions(
                model=first_model,
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                model_fallbacks=(fallback_model,),
                request_log_routing=recovered_routing,
            )
            recovered_chunks = [chunk async for chunk in recovered_result]

        self.assertTrue(first_chunks)
        self.assertTrue(recovered_chunks)
        self.assertEqual(
            calls,
            [
                ("token-1", fallback_payload_model),
                ("token-2", first_payload_model),
            ],
        )
        self.assertEqual(recovered_routing["resolved_model"], first_model)


if __name__ == "__main__":
    unittest.main()
