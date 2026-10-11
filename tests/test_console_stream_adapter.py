import unittest

from app.dataplane.reverse.protocol.xai_console_chat import (
    ConsoleStreamAdapter,
    raise_if_empty_console_response,
)
from app.platform.errors import UpstreamResponseEmptyError


class ConsoleStreamAdapterTests(unittest.TestCase):
    def test_text_output_is_not_empty(self):
        adapter = ConsoleStreamAdapter()

        self.assertEqual(
            adapter.feed(
                "response.output_text.delta",
                '{"delta":"hello"}',
            ),
            ["hello"],
        )
        self.assertEqual(adapter.full_text, "hello")
        self.assertFalse(adapter.has_non_text_output)
        self.assertFalse(adapter.is_empty_response)

    def test_tool_output_is_not_empty_without_visible_text(self):
        adapter = ConsoleStreamAdapter()

        adapter.feed(
            "response.output_item.added",
            '{"item":{"type":"function_call","name":"search"}}',
        )

        self.assertEqual(adapter.output_item_types, frozenset({"function_call"}))
        self.assertTrue(adapter.has_non_text_output)
        self.assertFalse(adapter.is_empty_response)

    def test_reasoning_activity_is_not_empty_without_visible_text(self):
        adapter = ConsoleStreamAdapter()

        adapter.feed(
            "response.reasoning_summary_text.delta",
            '{"delta":"thinking"}',
        )

        self.assertTrue(adapter.has_non_text_output)
        self.assertFalse(adapter.is_empty_response)

    def test_lifecycle_only_stream_is_empty(self):
        adapter = ConsoleStreamAdapter()

        adapter.feed(
            "response.output_item.added",
            '{"item":{"id":"msg_1","type":"message","content":[]}}',
        )
        adapter.feed(
            "response.content_part.added",
            '{"part":{"type":"output_text","text":""}}',
        )
        adapter.feed(
            "response.completed",
            '{"response":{"usage":{"input_tokens":1,"output_tokens":0}}}',
        )

        self.assertEqual(adapter.output_item_types, frozenset())
        self.assertFalse(adapter.has_non_text_output)
        self.assertTrue(adapter.is_empty_response)

        with self.assertRaises(UpstreamResponseEmptyError):
            raise_if_empty_console_response(adapter)


if __name__ == "__main__":
    unittest.main()
