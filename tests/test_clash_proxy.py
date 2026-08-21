import unittest
from unittest.mock import patch

from app.control.proxy import ProxyDirectory
from app.control.proxy.bridge import (
    build_mihomo_config,
    build_sing_box_config,
    build_xray_config,
)
from app.control.proxy.clash import (
    ClashParseError,
    find_clash_candidate,
    parse_clash_yaml,
)
from app.control.proxy.kernels import KernelSpec, ProxyKernelManager


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

    def test_parse_native_and_kernel_nodes(self):
        candidates = parse_clash_yaml(self.sample_yaml)

        self.assertEqual(len(candidates), 3)
        self.assertTrue(candidates[0].supported)
        self.assertEqual(
            candidates[0].proxy_url,
            "http://user%40example.com:p%40ss%20word@proxy.example.com:8080",
        )
        self.assertTrue(candidates[1].supported)
        self.assertEqual(candidates[1].proxy_url, "socks5://127.0.0.1:7891")
        self.assertTrue(candidates[2].supported)
        self.assertEqual(candidates[2].kernels, ("xray", "mihomo", "sing-box"))

    def test_find_candidate_accepts_kernel_node(self):
        candidates = parse_clash_yaml(self.sample_yaml)

        selected = find_clash_candidate(self.sample_yaml, candidates[1].proxy_id)
        self.assertEqual(selected.name, "SOCKS 节点")

        selected_kernel_node = find_clash_candidate(self.sample_yaml, candidates[2].proxy_id)
        self.assertEqual(selected_kernel_node.proxy_type, "vmess")

    def test_invalid_yaml_and_missing_proxies_are_rejected(self):
        with self.assertRaises(ClashParseError):
            parse_clash_yaml("proxies: [")
        with self.assertRaises(ClashParseError):
            parse_clash_yaml("mixed-port: 7890")

    def test_shadowsocks_plugin_requires_mihomo(self):
        candidates = parse_clash_yaml(
            """
proxies:
  - name: SS plugin
    type: ss
    server: ss.example.com
    port: 443
    cipher: aes-128-gcm
    password: secret
    plugin: v2ray-plugin
    plugin-opts:
      mode: websocket
"""
        )
        self.assertEqual(candidates[0].kernels, ("mihomo",))


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

    async def test_kernel_node_uses_local_bridge_url(self):
        fake_config = _FakeProxyConfig()
        fake_config.values.update(
            {
                "proxy.egress.mode": "direct",
                "proxy.egress.proxy_url": "",
                "proxy.clash.yaml": ClashProxyParserTests.sample_yaml,
                "proxy.clash.selected_proxy_id": parse_clash_yaml(
                    ClashProxyParserTests.sample_yaml
                )[2].proxy_id,
                "proxy.clash.selected_kernel": "xray",
                "proxy.clash.selected_url": "",
            }
        )

        class _FakeBridgeManager:
            async def ensure_candidate(self, candidate, preferred, *, auto_download):
                self.candidate = candidate
                self.preferred = preferred
                return "socks5://127.0.0.1:19001", "xray"

        bridge = _FakeBridgeManager()
        directory = ProxyDirectory()
        with (
            patch("app.control.proxy.get_config", return_value=fake_config),
            patch("app.control.proxy.get_proxy_bridge_manager", return_value=bridge),
        ):
            await directory.load()

        self.assertEqual(directory.nodes[0].proxy_url, "socks5://127.0.0.1:19001")
        self.assertEqual(bridge.candidate.proxy_type, "vmess")


class ProxyKernelConfigTests(unittest.TestCase):
    node = {
        "name": "VMess WS",
        "type": "vmess",
        "server": "vmess.example.com",
        "port": 443,
        "uuid": "00000000-0000-0000-0000-000000000000",
        "alterId": 0,
        "cipher": "auto",
        "tls": True,
        "servername": "cdn.example.com",
        "network": "ws",
        "ws-opts": {"path": "/gateway", "headers": {"Host": "cdn.example.com"}},
    }

    def test_all_kernel_configs_have_single_proxy_route(self):
        xray = build_xray_config(self.node, 19001)
        sing_box = build_sing_box_config(self.node, 19002)
        mihomo = build_mihomo_config(self.node, 19003)

        self.assertEqual(xray["inbounds"][0]["port"], 19001)
        self.assertEqual(xray["outbounds"][0]["protocol"], "vmess")
        self.assertEqual(sing_box["route"]["final"], "proxy-out")
        self.assertEqual(sing_box["outbounds"][0]["transport"]["type"], "ws")
        self.assertEqual(mihomo["mixed-port"], 19003)
        self.assertEqual(mihomo["rules"], ["MATCH,proxy-out"])

    def test_release_asset_selection_does_not_mix_architectures(self):
        spec = KernelSpec("mihomo", "Mihomo", "MetaCubeX/mihomo", "mihomo", "MIHOMO_BINARY_PATH")
        assets = [
            {"name": "mihomo-linux-arm64-v1.0.0.gz", "browser_download_url": "arm", "size": 1},
            {"name": "mihomo-linux-amd64-compatible-v1.0.0.gz", "browser_download_url": "amd", "size": 1},
        ]
        selected = ProxyKernelManager._select_asset(spec, assets, "linux", "amd64")
        self.assertEqual(selected.url, "amd")


if __name__ == "__main__":
    unittest.main()
