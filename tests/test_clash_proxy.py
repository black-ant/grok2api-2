import unittest
from unittest.mock import patch

from app.control.proxy import ProxyDirectory
from app.control.proxy.clash import (
    ClashParseError,
    find_clash_candidate,
    parse_clash_yaml,
)


class ClashProxyParserTests(unittest.TestCase):
    sample_yaml = """
proxies:
  - name: HTTP 节点
    type: http
    server: proxy.example.com
    port: 8080
    username: user@example.com
    password: p@ss word
  - name: SOCKS 节点
    type: socks5
    server: 127.0.0.1
    port: 7891
  - name: VMess 节点
    type: vmess
    server: vmess.example.com
    port: 443
    uuid: 00000000-0000-0000-0000-000000000000
"""

    def test_parse_native_and_unsupported_nodes(self):
        candidates = parse_clash_yaml(self.sample_yaml)

        self.assertEqual(len(candidates), 3)
        self.assertTrue(candidates[0].supported)
        self.assertEqual(
            candidates[0].proxy_url,
            "http://user%40example.com:p%40ss%20word@proxy.example.com:8080",
        )
        self.assertTrue(candidates[1].supported)
        self.assertEqual(candidates[1].proxy_url, "socks5://127.0.0.1:7891")
        self.assertFalse(candidates[2].supported)
        self.assertIn("Mihomo/Xray", candidates[2].reason)

    def test_find_candidate_requires_supported_id(self):
        candidates = parse_clash_yaml(self.sample_yaml)

        selected = find_clash_candidate(self.sample_yaml, candidates[1].proxy_id)
        self.assertEqual(selected.name, "SOCKS 节点")

        with self.assertRaises(ClashParseError):
            find_clash_candidate(self.sample_yaml, candidates[2].proxy_id)

    def test_invalid_yaml_and_missing_proxies_are_rejected(self):
        with self.assertRaises(ClashParseError):
            parse_clash_yaml("proxies: [")
        with self.assertRaises(ClashParseError):
            parse_clash_yaml("mixed-port: 7890")


class _FakeProxyConfig:
    def __init__(self):
        self.values = {
            "proxy.egress.mode": "single_proxy",
            "proxy.egress.proxy_url": "http://manual.example.com:8080",
            "proxy.egress.resource_proxy_url": "",
            "proxy.egress.proxy_pool": [],
            "proxy.egress.resource_proxy_pool": [],
            "proxy.clash.enabled": True,
            "proxy.clash.yaml": ClashProxyParserTests.sample_yaml,
            "proxy.clash.selected_proxy_id": "",
            "proxy.clash.selected_url": "socks5://127.0.0.1:7891",
            "proxy.clearance.mode": "none",
        }

    def get_bool(self, key, default=False):
        return self.values.get(key, default)

    def get_str(self, key, default=""):
        return self.values.get(key, default)

    def get_list(self, key, default=None):
        return self.values.get(key, [] if default is None else default)

    def get_int(self, key, default=0):
        return self.values.get(key, default)


class ClashProxyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_clash_overrides_and_disabled_clash_restores_egress(self):
        fake_config = _FakeProxyConfig()
        directory = ProxyDirectory()
        with patch("app.control.proxy.get_config", return_value=fake_config):
            await directory.load()
            self.assertEqual(directory.nodes[0].proxy_url, "socks5://127.0.0.1:7891")

            fake_config.values["proxy.clash.enabled"] = False
            await directory.load()

        self.assertEqual(directory.nodes[0].proxy_url, "http://manual.example.com:8080")


if __name__ == "__main__":
    unittest.main()
