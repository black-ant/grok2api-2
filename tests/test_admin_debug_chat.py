import unittest
from pathlib import Path


class AdminDebugChatPageTests(unittest.TestCase):
    def test_debug_chat_sends_external_v1_request(self):
        html = Path("app/statics/admin/debug-chat.html").read_text(encoding="utf-8")

        self.assertIn("externalFetch('/v1/chat/completions'", html)
        self.assertNotIn("const result = await apiFetch('/debug/chat'", html)
        self.assertNotIn("token: selectedTokenValue()", html)

    def test_external_auth_select_is_enabled_after_load(self):
        html = Path("app/statics/admin/debug-chat.html").read_text(encoding="utf-8")

        self.assertIn("select.disabled = false", html)
        self.assertIn("externalApiKeys = apiKeys(cfg?.app?.api_key)", html)
        self.assertIn("token-select').addEventListener('change'", html)

    def test_debug_chat_can_select_a_proxy_without_changing_global_route(self):
        html = Path("app/statics/admin/debug-chat.html").read_text(encoding="utf-8")

        self.assertIn('id="proxy-select"', html)
        self.assertIn("apiFetch('/proxy/clash')", html)
        self.assertIn("payload.pool_proxies", html)
        self.assertNotIn("(payload.proxies || []).filter((item) => item.supported)", html)
        self.assertIn("apiFetch('/debug/chat', requestOptions)", html)
        self.assertIn("proxy_id: proxyId", html)
        self.assertIn("'<option value=\"\">跟随全局代理</option>'", html)

    def test_debug_chat_marks_virtual_models(self):
        html = Path("app/statics/admin/debug-chat.html").read_text(encoding="utf-8")

        self.assertIn("item.virtual ? '虚拟模型'", html)


if __name__ == "__main__":
    unittest.main()
