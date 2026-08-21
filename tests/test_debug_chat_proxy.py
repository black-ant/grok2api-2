import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.control.proxy import ProxyDirectory, acquire_clash_proxy_lease
from app.control.proxy.clash import ClashParseError, parse_clash_yaml
from app.control.proxy.models import ProxyLease
from app.control.model import registry as model_registry
from app.products.web.admin import debug_chat


class _ProxyConfig:
    def __init__(self, raw_yaml):
        self.values = {
            "proxy.clash.draft_yaml": raw_yaml,
            "proxy.clash.yaml": raw_yaml,
            "proxy.clash.pool_kernels": {},
            "proxy.kernels.auto_download": True,
        }

    def get_bool(self, key, default=False):
        return self.values.get(key, default)

    def get_str(self, key, default=""):
        return self.values.get(key, default)

    def get(self, key, default=None):
        return self.values.get(key, default)


class _ProxyBridge:
    def __init__(self):
        self.calls = []

    async def ensure_candidate(self, candidate, preferred, *, auto_download):
        self.calls.append((candidate, preferred, auto_download))
        return "socks5://127.0.0.1:19001", "xray"


class _Request:
    def __init__(self, payload):
        self.payload = payload
        self.state = SimpleNamespace()

    async def json(self):
        return dict(self.payload)


class DebugChatProxyTests(unittest.IsolatedAsyncioTestCase):
    sample_yaml = """
proxies:
  - name: VMess test node
    type: vmess
    server: vmess.example.com
    port: 443
    uuid: 00000000-0000-0000-0000-000000000000
"""

    async def test_clash_selection_prepares_environment_matched_kernel(self):
        proxy_id = parse_clash_yaml(self.sample_yaml)[0].proxy_id
        config = _ProxyConfig(self.sample_yaml)
        bridge = _ProxyBridge()
        directory = ProxyDirectory()

        with (
            patch("app.control.proxy.get_config", return_value=config),
            patch("app.control.proxy.get_proxy_bridge_manager", return_value=bridge),
            patch(
                "app.control.proxy.get_proxy_directory",
                new=AsyncMock(return_value=directory),
            ),
        ):
            lease, kernel = await acquire_clash_proxy_lease(proxy_id)

        self.assertEqual(kernel, "xray")
        self.assertEqual(lease.proxy_url, "socks5://127.0.0.1:19001")
        self.assertTrue(lease.proxy_override)
        self.assertEqual(bridge.calls[0][1:], (None, True))

        with self.assertRaises(ClashParseError):
            await acquire_clash_proxy_lease("clash-missing")

    async def test_selected_proxy_lease_carries_log_metadata(self):
        proxy_id = parse_clash_yaml(self.sample_yaml)[0].proxy_id
        config = _ProxyConfig(self.sample_yaml)
        config.values['proxy.clash.speed_results'] = {
            proxy_id: {
                'state': 'alive',
                'egress_ip': '203.0.113.7',
            },
        }
        bridge = _ProxyBridge()
        directory = ProxyDirectory()

        with (
            patch('app.control.proxy.get_config', return_value=config),
            patch('app.control.proxy.get_proxy_bridge_manager', return_value=bridge),
            patch(
                'app.control.proxy.get_proxy_directory',
                new=AsyncMock(return_value=directory),
            ),
        ):
            lease, kernel = await acquire_clash_proxy_lease(proxy_id)

        self.assertEqual(kernel, 'xray')
        self.assertEqual(lease.proxy_id, proxy_id)
        self.assertEqual(lease.proxy_name, 'VMess test node')
        self.assertEqual(lease.proxy_server, 'vmess.example.com')
        self.assertEqual(lease.proxy_port, 443)
        self.assertEqual(lease.proxy_kernel, 'xray')
        self.assertEqual(lease.egress_ip, '203.0.113.7')
        self.assertEqual(lease.request_log_proxy()['egress_ip'], '203.0.113.7')

    async def test_selected_proxy_is_forwarded_as_request_scoped_lease(self):
        lease = ProxyLease(
            lease_id="lease-1",
            proxy_url="http://selected.example.com:8080",
            proxy_override=True,
        )
        captured = {}

        async def fake_chat_completions(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        with (
            patch(
                "app.products.web.admin.acquire_clash_proxy_lease",
                new=AsyncMock(return_value=(lease, "native")),
            ),
            patch(
                "app.products.openai.router._resolve_model_for_request",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        model="grok-4.3",
                        spec=model_registry.get("grok-4.3"),
                        is_virtual=False,
                        pool="stable",
                    )
                ),
            ),
            patch(
                "app.products.openai.chat.completions",
                new=AsyncMock(side_effect=fake_chat_completions),
            ),
        ):
            request = _Request(
                    {
                        "model": "grok-4.3",
                        "messages": [{"role": "user", "content": "hi"}],
                        "proxy_id": "clash-selected",
                    }
            )
            response = await debug_chat(request)

        self.assertIs(captured["proxy_lease"], lease)
        self.assertEqual(captured["stream"], False)
        self.assertEqual(request.state.request_log_routing["proxy"]["server"], "selected.example.com")
        self.assertIn('"proxy_id":"clash-selected"', response.body.decode())

    async def test_no_proxy_selection_keeps_the_default_lease_path(self):
        captured = {}

        async def fake_chat_completions(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        with patch(
            "app.products.openai.router._resolve_model_for_request",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    model="grok-4.3",
                    spec=model_registry.get("grok-4.3"),
                    is_virtual=False,
                    pool="stable",
                )
            ),
        ), patch(
            "app.products.openai.chat.completions",
            new=AsyncMock(side_effect=fake_chat_completions),
        ):
            await debug_chat(
                _Request(
                    {
                        "model": "grok-4.3",
                        "messages": [{"role": "user", "content": "hi"}],
                    }
                )
            )

        self.assertIsNone(captured["proxy_lease"])

    async def test_virtual_model_is_resolved_before_chat_service(self):
        captured = {}
        resolved = SimpleNamespace(
            model="grok-4.3-console",
            spec=model_registry.get("grok-4.3-console"),
            is_virtual=True,
            pool="stable",
            candidates=("grok-4.3-console", "grok-4.20-0309-console"),
        )

        async def fake_chat_completions(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        with (
            patch(
                "app.products.openai.router._resolve_model_for_request",
                new=AsyncMock(return_value=resolved),
            ),
            patch(
                "app.products.openai.chat.completions",
                new=AsyncMock(side_effect=fake_chat_completions),
            ),
        ):
            request = _Request(
                {
                    "model": "FREE",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
            await debug_chat(request)

        self.assertEqual(captured["model"], "grok-4.3-console")
        self.assertEqual(captured["model_fallbacks"], ("grok-4.20-0309-console",))
        self.assertEqual(
            request.state.request_log_routing["model"],
            "FREE",
        )
        self.assertEqual(
            request.state.request_log_routing["resolved_model"],
            "grok-4.3-console",
        )


if __name__ == "__main__":
    unittest.main()
