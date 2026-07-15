import unittest

from app.dataplane.reverse.protocol.xai_chat import raise_for_stream_error
from app.platform.errors import UpstreamError


class XaiChatStreamErrorTests(unittest.TestCase):
    def test_chat_upstream_returned_403_keeps_status(self):
        payload = {
            "error": {
                "message": "Chat upstream returned 403",
                "type": "upstream_error",
                "code": "upstream_error",
            }
        }

        with self.assertRaises(UpstreamError) as caught:
            raise_for_stream_error(payload)

        self.assertEqual(caught.exception.status, 403)

    def test_rate_limit_stream_error_stays_429(self):
        payload = {
            "error": {
                "message": "Too many requests",
                "type": "upstream_error",
                "code": 8,
            }
        }

        with self.assertRaises(UpstreamError) as caught:
            raise_for_stream_error(payload)

        self.assertEqual(caught.exception.status, 429)


if __name__ == "__main__":
    unittest.main()
