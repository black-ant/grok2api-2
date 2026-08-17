import asyncio
import unittest

from app.dataplane.reverse.transport.semantic_idle import (
    is_chat_stream_activity,
    is_console_response_activity,
    with_semantic_idle_timeout,
)
from app.platform.errors import StreamIdleTimeout, UpstreamError


class SemanticIdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonsemantic_chat_frames_eventually_timeout(self):
        async def source():
            while True:
                yield 'data: {"result":{"response":{"messageTag":"control"}}}'
                await asyncio.sleep(0.005)

        with self.assertRaises(StreamIdleTimeout):
            async for _item in with_semantic_idle_timeout(
                source(), 0.03, is_chat_stream_activity
            ):
                pass

    async def test_semantic_chat_frames_reset_deadline(self):
        async def source():
            yield 'data: {"result":{"response":{"token":"first"}}}'
            await asyncio.sleep(0.05)
            yield 'data: {"result":{"response":{"token":"second"}}}'
            await asyncio.sleep(0.05)

        items = [
            item
            async for item in with_semantic_idle_timeout(
                source(), 0.08, is_chat_stream_activity
            )
        ]
        self.assertEqual(len(items), 2)

    async def test_console_generated_events_reset_deadline(self):
        async def source():
            yield (
                "response.in_progress",
                '{"type":"response.in_progress"}',
            )
            await asyncio.sleep(0.05)
            yield (
                "response.output_text.delta",
                '{"type":"response.output_text.delta","delta":"x"}',
            )
            await asyncio.sleep(0.05)

        items = [
            item
            async for item in with_semantic_idle_timeout(
                source(), 0.08, is_console_response_activity
            )
        ]
        self.assertEqual(len(items), 2)

    def test_chat_activity_requires_generated_content(self):
        self.assertFalse(is_chat_stream_activity(": keep-alive"))
        self.assertFalse(
            is_chat_stream_activity(
                'data: {"result":{"response":{"token":""}}}'
            )
        )
        self.assertTrue(
            is_chat_stream_activity(
                'data: {"result":{"response":{"token":"x"}}}'
            )
        )

    def test_console_activity_ignores_lifecycle_and_empty_delta(self):
        self.assertFalse(
            is_console_response_activity(
                ("response.in_progress", '{"type":"response.in_progress"}')
            )
        )
        self.assertFalse(
            is_console_response_activity(
                (
                    "response.output_text.delta",
                    '{"type":"response.output_text.delta","delta":""}',
                )
            )
        )
        self.assertTrue(
            is_console_response_activity(
                (
                    "response.output_item.added",
                    '{"type":"response.output_item.added",'
                    '"item":{"id":"call_1","type":"web_search_call"}}',
                )
            )
        )

    def test_stream_idle_timeout_is_an_upstream_error(self):
        error = StreamIdleTimeout(3)
        self.assertIsInstance(error, UpstreamError)
        self.assertEqual(error.status, 504)
        self.assertEqual(error.code, "stream_idle_timeout")


if __name__ == "__main__":
    unittest.main()