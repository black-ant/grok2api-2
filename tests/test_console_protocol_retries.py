import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.products.anthropic import console_messages
from app.products.openai import console_responses


class _Config:
    def get_float(self, _key, default=0.0):
        return default


class ConsoleProtocolRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_retry_reuses_stream_prefix(self):
        accounts = iter([
            SimpleNamespace(token="token-1", pool_id=0),
            SimpleNamespace(token="token-2", pool_id=0),
        ])
        calls = []

        async def fake_reserve(*_args, **_kwargs):
            return next(accounts), 5

        async def fake_stream(token, _payload, timeout_s):
            calls.append((token, timeout_s))
            if token == "token-1":
                yield "response.completed", '{"response":{"usage":{"output_tokens":0}}}'
                return
            yield "response.output_text.delta", '{"delta":"ok"}'
            yield "response.completed", '{"response":{"usage":{"output_tokens":1}}}'

        directory = SimpleNamespace(
            release=AsyncMock(),
            feedback=AsyncMock(),
        )

        with patch.object(console_responses, "get_config", return_value=_Config()), patch.object(
            console_responses, "selection_max_retries", return_value=1
        ), patch.object(
            console_responses, "_configured_retry_codes", return_value=frozenset()
        ), patch.object(
            console_responses, "reserve_account", new=AsyncMock(side_effect=fake_reserve)
        ), patch.object(
            console_responses, "stream_console_chat", new=fake_stream
        ), patch.object(
            console_responses, "get_refresh_service", return_value=None
        ), patch(
            "app.dataplane.account._directory", directory
        ):
            result = await console_responses.create(
                model="grok-4.20-0309-reasoning-console",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                emit_think=False,
                temperature=0.7,
                top_p=0.95,
                response_id="resp_1",
                reasoning_id="rs_1",
                message_id="msg_1",
            )
            chunks = [chunk async for chunk in result]

        self.assertEqual([token for token, _ in calls], ["token-1", "token-2"])
        self.assertEqual(sum("event: response.created\n" in chunk for chunk in chunks), 1)
        self.assertEqual(sum("event: response.output_item.added\n" in chunk for chunk in chunks), 1)
        self.assertTrue(any('"delta":"ok"' in chunk for chunk in chunks))

    async def test_anthropic_retry_reuses_stream_prefix(self):
        accounts = iter([
            SimpleNamespace(token="token-1", pool_id=0),
            SimpleNamespace(token="token-2", pool_id=0),
        ])
        calls = []

        async def fake_reserve(*_args, **_kwargs):
            return next(accounts), 5

        async def fake_stream(token, _payload, timeout_s):
            calls.append((token, timeout_s))
            if token == "token-1":
                yield "response.completed", '{"response":{"usage":{"output_tokens":0}}}'
                return
            yield "response.output_text.delta", '{"delta":"ok"}'
            yield "response.completed", '{"response":{"usage":{"output_tokens":1}}}'

        directory = SimpleNamespace(
            release=AsyncMock(),
            feedback=AsyncMock(),
        )

        with patch.object(console_messages, "get_config", return_value=_Config()), patch.object(
            console_messages, "selection_max_retries", return_value=1
        ), patch.object(
            console_messages, "_configured_retry_codes", return_value=frozenset()
        ), patch.object(
            console_messages, "reserve_account", new=AsyncMock(side_effect=fake_reserve)
        ), patch.object(
            console_messages, "stream_console_chat", new=fake_stream
        ), patch.object(
            console_messages, "get_refresh_service", return_value=None
        ), patch(
            "app.dataplane.account._directory", directory
        ):
            result = await console_messages.create(
                model="grok-4.20-0309-reasoning-console",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                emit_think=False,
                temperature=0.7,
                top_p=0.95,
                msg_id="msg_1",
            )
            chunks = [chunk async for chunk in result]

        self.assertEqual([token for token, _ in calls], ["token-1", "token-2"])
        self.assertEqual(sum("event: message_start\n" in chunk for chunk in chunks), 1)
        self.assertEqual(sum("event: content_block_start\n" in chunk for chunk in chunks), 1)
        self.assertTrue(any('"text":"ok"' in chunk for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
