import unittest
from unittest.mock import patch

from app.dataplane.reverse.protocol.xai_console_chat import (
    ConsoleStreamAdapter,
    raise_if_empty_console_response,
    raise_if_incomplete_console_response,
)
from app.platform.errors import (
    UpstreamResponseEmptyError,
    UpstreamStreamIncompleteError,
)
from app.products._stream_guard import enforce_stream_terminal, require_stream_terminal


class _ConfigStub:
    def __init__(self, required):
        self._required = required

    def get_bool(self, key, default=False):
        return self._required


def _feed(adapter: ConsoleStreamAdapter, event_type: str, data: str) -> list[str]:
    return adapter.feed(event_type, data)


class ConsoleTerminalTests(unittest.TestCase):
    def test_completed_marks_terminal(self):
        adapter = ConsoleStreamAdapter()
        _feed(adapter, "response.output_text.delta", '{"delta":"hi"}')
        _feed(adapter, "response.completed", '{"response":{"usage":{}}}')

        self.assertTrue(adapter.terminal_seen)
        raise_if_empty_console_response(adapter)
        raise_if_incomplete_console_response(adapter)

    def test_incomplete_and_failed_count_as_terminals(self):
        for event_type in ("response.incomplete", "response.failed"):
            adapter = ConsoleStreamAdapter()
            _feed(adapter, "response.output_text.delta", '{"delta":"hi"}')
            _feed(adapter, event_type, "{}")
            self.assertTrue(adapter.terminal_seen, event_type)
            raise_if_incomplete_console_response(adapter)

    def test_text_without_terminal_is_incomplete(self):
        adapter = ConsoleStreamAdapter()
        _feed(adapter, "response.output_text.delta", '{"delta":"partial"}')

        with self.assertRaises(UpstreamStreamIncompleteError) as ctx:
            raise_if_incomplete_console_response(adapter)
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "upstream_stream_incomplete")

    def test_empty_stream_is_not_reported_as_incomplete(self):
        adapter = ConsoleStreamAdapter()

        with self.assertRaises(UpstreamResponseEmptyError):
            raise_if_empty_console_response(adapter)
        # The empty guard owns this case; the truncation guard stays silent so the
        # account-retry path keeps its empty-response semantics.
        raise_if_incomplete_console_response(adapter)


class WebStreamGuardTests(unittest.TestCase):
    def test_guard_disabled_by_default(self):
        with patch(
            "app.products._stream_guard.get_config",
            return_value=_ConfigStub(False),
        ):
            self.assertFalse(require_stream_terminal())
            enforce_stream_terminal(False, protocol="chat")

    def test_guard_raises_when_terminal_missing(self):
        with patch(
            "app.products._stream_guard.get_config",
            return_value=_ConfigStub(True),
        ):
            with self.assertRaises(UpstreamStreamIncompleteError) as ctx:
                enforce_stream_terminal(False, protocol="chat")
            self.assertIn("chat", str(ctx.exception))

    def test_guard_accepts_completed_stream(self):
        with patch(
            "app.products._stream_guard.get_config",
            return_value=_ConfigStub(True),
        ):
            enforce_stream_terminal(True, protocol="messages")

    def test_guard_falls_back_to_disabled_without_config(self):
        with patch(
            "app.products._stream_guard.get_config",
            side_effect=RuntimeError("no config"),
        ):
            self.assertFalse(require_stream_terminal())


if __name__ == "__main__":
    unittest.main()
