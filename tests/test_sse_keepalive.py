import asyncio
import unittest
from unittest.mock import patch

from app.products._sse_keepalive import (
    KEEPALIVE_FRAME,
    keepalive_interval_s,
    keepalive_stream,
    with_keepalive,
)


class _ConfigStub:
    def __init__(self, value):
        self._value = value

    def get_float(self, key, default=0.0):
        return self._value


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_keepalive_fills_silence_and_preserves_order(self):
        async def source():
            yield "data: first\n\n"
            await asyncio.sleep(0.08)
            yield "data: second\n\n"

        items = [item async for item in with_keepalive(source(), 0.02)]
        self.assertEqual(items[0], "data: first\n\n")
        self.assertEqual(items[-1], "data: second\n\n")
        self.assertGreaterEqual(items.count(KEEPALIVE_FRAME), 2)
        self.assertEqual(
            [item for item in items if item != KEEPALIVE_FRAME],
            ["data: first\n\n", "data: second\n\n"],
        )

    async def test_keepalive_does_not_cancel_slow_source(self):
        async def source():
            await asyncio.sleep(0.06)
            yield "data: only\n\n"

        items = [item async for item in with_keepalive(source(), 0.01)]
        self.assertEqual(items[-1], "data: only\n\n")
        self.assertTrue(all(item == KEEPALIVE_FRAME for item in items[:-1]))

    async def test_keepalive_disabled_passes_items_through(self):
        async def source():
            await asyncio.sleep(0.03)
            yield "data: only\n\n"

        items = [item async for item in with_keepalive(source(), 0)]
        self.assertEqual(items, ["data: only\n\n"])

    async def test_keepalive_propagates_source_errors(self):
        async def source():
            await asyncio.sleep(0.02)
            raise RuntimeError("boom")
            yield ""  # pragma: no cover

        with self.assertRaises(RuntimeError):
            async for _item in with_keepalive(source(), 0.01):
                pass

    async def test_keepalive_stops_with_empty_source(self):
        async def source():
            return
            yield ""  # pragma: no cover

        items = [item async for item in with_keepalive(source(), 0.01)]
        self.assertEqual(items, [])

    async def test_keepalive_stream_uses_configured_interval(self):
        async def source():
            await asyncio.sleep(0.05)
            yield "data: only\n\n"

        with patch(
            "app.products._sse_keepalive.get_config",
            return_value=_ConfigStub(0.01),
        ):
            self.assertAlmostEqual(keepalive_interval_s(), 0.01)
            items = [item async for item in keepalive_stream(source())]

        self.assertEqual(items[-1], "data: only\n\n")
        self.assertIn(KEEPALIVE_FRAME, items)

    async def test_keepalive_stream_disabled_passes_source_through(self):
        async def source():
            yield "data: only\n\n"

        with patch(
            "app.products._sse_keepalive.get_config",
            return_value=_ConfigStub(0),
        ):
            wrapped = keepalive_stream(source())

        items = [item async for item in wrapped]
        self.assertEqual(items, ["data: only\n\n"])

    def test_keepalive_interval_falls_back_without_config(self):
        with patch(
            "app.products._sse_keepalive.get_config",
            side_effect=RuntimeError("no config"),
        ):
            self.assertEqual(keepalive_interval_s(), 15.0)


class RouterWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_sse_commits_to_stream_after_idle_upstream(self):
        from fastapi.responses import StreamingResponse

        from app.products.openai.router import _sse_response_or_error

        async def source():
            await asyncio.sleep(0.05)
            yield "data: real\n\n"

        with patch(
            "app.products._sse_keepalive.get_config",
            return_value=_ConfigStub(0.01),
        ):
            response = await _sse_response_or_error(source())
            self.assertIsInstance(response, StreamingResponse)
            chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(chunks[0], KEEPALIVE_FRAME)
        self.assertEqual(chunks[-1], "data: real\n\n")

    async def test_responses_sse_emits_keepalive_frames(self):
        from app.products.openai.router import _safe_sse_responses

        async def source():
            await asyncio.sleep(0.04)
            yield "event: response.created\ndata: {}\n\n"

        with patch(
            "app.products._sse_keepalive.get_config",
            return_value=_ConfigStub(0.01),
        ):
            chunks = [chunk async for chunk in _safe_sse_responses(source())]

        self.assertIn(KEEPALIVE_FRAME, chunks)
        self.assertEqual(chunks[-1], "event: response.created\ndata: {}\n\n")

    async def test_anthropic_sse_emits_keepalive_frames(self):
        from app.products.anthropic.router import _safe_sse_anthropic

        async def source():
            await asyncio.sleep(0.04)
            yield "event: message_start\ndata: {}\n\n"

        with patch(
            "app.products._sse_keepalive.get_config",
            return_value=_ConfigStub(0.01),
        ):
            chunks = [chunk async for chunk in _safe_sse_anthropic(source())]

        self.assertIn(KEEPALIVE_FRAME, chunks)
        self.assertEqual(chunks[-1], "event: message_start\ndata: {}\n\n")


if __name__ == "__main__":
    unittest.main()
