import unittest
from pathlib import Path


class AdminDebugChatPageTests(unittest.TestCase):
    def test_debug_chat_sends_external_v1_request(self):
        html = Path("app/statics/admin/debug-chat.html").read_text(encoding="utf-8")

        self.assertIn("externalFetch('/v1/chat/completions'", html)
        self.assertNotIn("const result = await apiFetch('/debug/chat'", html)
        self.assertNotIn("token: selectedTokenValue()", html)


if __name__ == "__main__":
    unittest.main()
