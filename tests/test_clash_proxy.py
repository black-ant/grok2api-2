import asyncio
import json
import socket
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.control.proxy import ProxyDirectory
from app.control.proxy.bridge import (
    KernelBridgeError,
    ProxyKernelBridgeManager,
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
from app.control.proxy.speedtest import (
    iter_clash_candidate_results,
    speedtest_config,
    test_clash_candidate as run_clash_candidate,
)
from app.products.web.admin import proxy as admin_proxy


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

    def get_float(self, key, default=0.0):
        return self.values.get(key, default)

    def get(self, key, default=None):
        return self.values.get(key, default)


class _FakeAdminConfig(_FakeProxyConfig):
    def __init__(self, values=None):
        super().__init__()
        self.values.update(values or {})

    async def update(self, patch):
        for key, value in patch.get("proxy", {}).get("clash", {}).items():
            self.values[f"proxy.clash.{key}"] = value

    async def load(self):
        return None


class _FakeAdminBridge:
    def __init__(self):
        self.candidate = None
        self.candidates = []
        self.preferred = None
        self.preferreds = []
        self.stopped = False

    def statuses(self):
        return []

    def stop_all(self):
        self.stopped = True

    async def ensure_candidate(self, candidate, preferred, *, auto_download):
        self.candidate = candidate
        self.candidates.append(candidate)
        self.preferred = preferred
        self.preferreds.append(preferred)
        selected_kernel = preferred or candidate.kernels[0]
        return candidate.proxy_url or "socks5://127.0.0.1:19001", selected_kernel


class ClashAdminPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_persists_draft_and_state_restores_all_nodes(self):
        old_yaml = """
proxies:
  - name: Old node
    type: http
    server: old.example.com
    port: 8080
"""
        fake_config = _FakeAdminConfig(
            {
                "proxy.clash.yaml": old_yaml.strip(),
                "proxy.clash.enabled": False,
            }
        )
        bridge = _FakeAdminBridge()

        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
        ):
            parsed = await admin_proxy.parse_clash(
                admin_proxy.ClashParseRequest(yaml=ClashProxyParserTests.sample_yaml)
            )
            state = await admin_proxy.get_clash_state()

        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        self.assertEqual(parsed["yaml"], saved_yaml)
        self.assertEqual(fake_config.values["proxy.clash.draft_yaml"], saved_yaml)
        self.assertEqual(fake_config.values["proxy.clash.yaml"], old_yaml.strip())
        self.assertFalse(fake_config.values["proxy.clash.enabled"])
        self.assertEqual(state["yaml"], saved_yaml)
        self.assertEqual(state["total"], 3)
        self.assertEqual(
            [proxy["name"] for proxy in state["proxies"]],
            ["HTTP 节点", "SOCKS 节点", "VMess 节点"],
        )

    async def test_parse_only_updates_draft_not_formal_pool_state(self):
        old_yaml = """
proxies:
  - name: Old node
    type: http
    server: old.example.com
    port: 8080
"""
        old_id = parse_clash_yaml(old_yaml)[0].proxy_id
        fake_config = _FakeAdminConfig(
            {
                "proxy.clash.yaml": old_yaml.strip(),
                "proxy.clash.draft_yaml": old_yaml.strip(),
                "proxy.clash.pool_proxy_ids": [old_id],
                "proxy.clash.pool_kernels": {old_id: "native"},
            }
        )
        bridge = _FakeAdminBridge()

        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
        ):
            await admin_proxy.parse_clash(
                admin_proxy.ClashParseRequest(yaml=ClashProxyParserTests.sample_yaml)
            )
            state = await admin_proxy.get_clash_state()

        self.assertEqual(state["committed_yaml"], old_yaml.strip())
        self.assertEqual([proxy["name"] for proxy in state["committed_proxies"]], ["Old node"])
        self.assertEqual([proxy["name"] for proxy in state["pool_proxies"]], ["Old node"])
        self.assertEqual(
            [proxy["name"] for proxy in state["proxies"]],
            ["HTTP 节点", "SOCKS 节点", "VMess 节点"],
        )

    async def test_apply_switches_saved_nodes_without_resubmitting_yaml(self):
        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        candidates = parse_clash_yaml(saved_yaml)
        fake_config = _FakeAdminConfig(
            {
                "proxy.clash.yaml": "old-active-yaml",
                "proxy.clash.draft_yaml": saved_yaml,
            }
        )
        bridge = _FakeAdminBridge()

        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
        ):
            payload = await admin_proxy.apply_clash(
                admin_proxy.ClashApplyRequest(
                    proxy_id=candidates[1].proxy_id,
                    selected_kernel="native",
                )
            )

        self.assertEqual(bridge.candidate.name, "SOCKS 节点")
        self.assertEqual(fake_config.values["proxy.clash.yaml"], saved_yaml)
        self.assertEqual(fake_config.values["proxy.clash.draft_yaml"], saved_yaml)
        self.assertEqual(payload["selected_proxy_name"], "SOCKS 节点")
        self.assertEqual(payload["total"], 3)

    async def test_apply_adds_a_batch_to_the_existing_pool(self):
        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        candidates = parse_clash_yaml(saved_yaml)
        fake_config = _FakeAdminConfig(
            {
                "proxy.clash.draft_yaml": saved_yaml,
                "proxy.clash.pool_proxy_ids": [candidates[0].proxy_id],
                "proxy.clash.pool_kernels": {candidates[0].proxy_id: "native"},
            }
        )
        bridge = _FakeAdminBridge()

        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
        ):
            payload = await admin_proxy.apply_clash(
                admin_proxy.ClashApplyRequest(
                    proxy_ids=[candidates[1].proxy_id],
                    selected_kernel="native",
                )
            )

        self.assertEqual(
            fake_config.values["proxy.clash.pool_proxy_ids"],
            [candidates[0].proxy_id, candidates[1].proxy_id],
        )
        self.assertEqual([candidate.name for candidate in bridge.candidates], [
            "HTTP 节点",
            "SOCKS 节点",
        ])
        self.assertEqual(payload["pool_size"], 2)
        self.assertTrue(all(proxy["in_pool"] for proxy in payload["proxies"][:2]))

    async def test_mixed_pool_forces_native_kernel_for_native_nodes(self):
        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        candidates = parse_clash_yaml(saved_yaml)
        fake_config = _FakeAdminConfig({"proxy.clash.draft_yaml": saved_yaml})
        bridge = _FakeAdminBridge()

        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
        ):
            await admin_proxy.apply_clash(
                admin_proxy.ClashApplyRequest(
                    proxy_ids=[candidates[0].proxy_id, candidates[2].proxy_id],
                    selected_kernel="xray",
                )
            )

        self.assertEqual(bridge.preferreds, ["native", "xray"])

    async def test_speed_test_persists_results(self):
        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        candidate = parse_clash_yaml(saved_yaml)[0]
        result = {
            "state": "alive",
            "latency_ms": 123,
            "status_code": 200,
            "kernel": "native",
            "error": "",
            "tested_at": 1000,
        }
        fake_config = _FakeAdminConfig({"proxy.clash.draft_yaml": saved_yaml})
        bridge = _FakeAdminBridge()
        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
            patch.object(
                admin_proxy,
                "speedtest_config",
                return_value=("https://example.com", 2.0, False, 1),
            ),
            patch.object(
                admin_proxy,
                "test_clash_candidates",
                new=AsyncMock(return_value={candidate.proxy_id: result}),
            ),
        ):
            payload = await admin_proxy.test_clash(
                admin_proxy.ClashSpeedTestRequest(proxy_ids=[candidate.proxy_id])
            )

        self.assertEqual(fake_config.values["proxy.clash.speed_results"][candidate.proxy_id], result)
        self.assertEqual(payload["proxies"][0]["speed"], result)

    async def test_speed_test_persists_failed_results_without_none_values(self):
        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        candidate = parse_clash_yaml(saved_yaml)[0]
        result = {
            "state": "dead",
            "latency_ms": None,
            "status_code": None,
            "kernel": "native",
            "error": "连接失败",
            "tested_at": 1000,
        }
        fake_config = _FakeAdminConfig({"proxy.clash.draft_yaml": saved_yaml})
        bridge = _FakeAdminBridge()
        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
            patch.object(
                admin_proxy,
                "speedtest_config",
                return_value=("https://example.com", 2.0, False, 1),
            ),
            patch.object(
                admin_proxy,
                "test_clash_candidates",
                new=AsyncMock(return_value={candidate.proxy_id: result}),
            ),
        ):
            payload = await admin_proxy.test_clash(
                admin_proxy.ClashSpeedTestRequest(proxy_ids=[candidate.proxy_id])
            )

        persisted = fake_config.values["proxy.clash.speed_results"][candidate.proxy_id]
        self.assertEqual(persisted["state"], "dead")
        self.assertEqual(persisted["latency_ms"], 0)
        self.assertEqual(persisted["status_code"], 0)
        self.assertNotIn(None, persisted.values())
        self.assertEqual(payload["results"][candidate.proxy_id], result)

    async def test_stream_speed_test_emits_each_result_before_done(self):
        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        candidates = parse_clash_yaml(saved_yaml)
        results = {
            candidates[0].proxy_id: {
                "state": "alive",
                "latency_ms": 123,
                "status_code": 200,
                "kernel": "native",
                "error": "",
                "tested_at": 1000,
            },
            candidates[1].proxy_id: {
                "state": "dead",
                "latency_ms": None,
                "status_code": None,
                "kernel": "native",
                "error": "连接失败",
                "tested_at": 1001,
            },
        }

        async def fake_results(*args, **kwargs):
            for candidate in candidates[:2]:
                yield candidate.proxy_id, results[candidate.proxy_id]

        fake_config = _FakeAdminConfig({"proxy.clash.draft_yaml": saved_yaml})
        bridge = _FakeAdminBridge()
        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
            patch.object(
                admin_proxy,
                "speedtest_config",
                return_value=("https://example.com", 2.0, False, 1),
            ),
            patch.object(admin_proxy, "iter_clash_candidate_results", fake_results),
        ):
            response = await admin_proxy.stream_clash_test(
                admin_proxy.ClashSpeedTestRequest(
                    yaml=saved_yaml,
                    proxy_ids=[candidate.proxy_id for candidate in candidates[:2]],
                )
            )
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)

        events = [
            json.loads(line[6:])
            for chunk in chunks
            for line in chunk.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [event["type"] for event in events],
            ["started", "result", "result", "done"],
        )
        self.assertEqual(
            [event["proxy_id"] for event in events[1:3]],
            [candidates[0].proxy_id, candidates[1].proxy_id],
        )
        self.assertEqual(events[1]["completed"], 1)
        self.assertEqual(events[2]["completed"], 2)
        self.assertEqual(events[3]["state"]["proxies"][1]["speed"]["state"], "dead")

    async def test_formal_pool_speed_test_removes_failed_nodes_and_stops_bridge(self):
        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        candidates = parse_clash_yaml(saved_yaml)[:2]
        results = {
            candidates[0].proxy_id: {"state": "alive", "latency_ms": 12},
            candidates[1].proxy_id: {"state": "dead", "latency_ms": None},
        }

        async def fake_results(*args, **kwargs):
            for candidate in candidates:
                yield candidate.proxy_id, results[candidate.proxy_id]

        class _PoolBridge(_FakeAdminBridge):
            def __init__(self):
                super().__init__()
                self.stopped_candidates = []

            def stop_candidate(self, candidate, kernel=None):
                self.stopped_candidates.append(candidate.proxy_id)

        failed_id = candidates[1].proxy_id
        alive_id = candidates[0].proxy_id
        fake_config = _FakeAdminConfig(
            {
                "proxy.clash.yaml": saved_yaml,
                "proxy.clash.draft_yaml": saved_yaml,
                "proxy.clash.enabled": True,
                "proxy.clash.pool_proxy_ids": [alive_id, failed_id],
                "proxy.clash.pool_kernels": {alive_id: "native", failed_id: "native"},
                "proxy.clash.selected_proxy_id": failed_id,
                "proxy.clash.selected_proxy_name": candidates[1].name,
            }
        )
        bridge = _PoolBridge()
        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
            patch.object(
                admin_proxy,
                "speedtest_config",
                return_value=("https://example.com", 2.0, False, 1),
            ),
            patch.object(admin_proxy, "iter_clash_candidate_results", fake_results),
        ):
            response = await admin_proxy.stream_clash_test(
                admin_proxy.ClashSpeedTestRequest(
                    proxy_ids=[alive_id, failed_id],
                    formal_pool=True,
                )
            )
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)

        events = [
            json.loads(line[6:])
            for chunk in chunks
            for line in chunk.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[-1]["removed_proxy_ids"], [failed_id])
        self.assertEqual(fake_config.values["proxy.clash.pool_proxy_ids"], [alive_id])
        self.assertTrue(fake_config.values["proxy.clash.enabled"])
        self.assertEqual(fake_config.values["proxy.clash.selected_proxy_id"], alive_id)
        self.assertEqual(bridge.stopped_candidates, [failed_id])

    async def test_formal_pool_speed_test_disables_proxy_when_all_nodes_fail(self):
        saved_yaml = ClashProxyParserTests.sample_yaml.strip()
        candidate = parse_clash_yaml(saved_yaml)[0]
        result = {"state": "dead", "latency_ms": None}

        async def fake_results(*args, **kwargs):
            yield candidate.proxy_id, result

        class _PoolBridge(_FakeAdminBridge):
            def __init__(self):
                super().__init__()
                self.stopped_candidates = []

            def stop_candidate(self, candidate, kernel=None):
                self.stopped_candidates.append(candidate.proxy_id)

        fake_config = _FakeAdminConfig(
            {
                "proxy.clash.yaml": saved_yaml,
                "proxy.clash.draft_yaml": saved_yaml,
                "proxy.clash.enabled": True,
                "proxy.clash.pool_proxy_ids": [candidate.proxy_id],
                "proxy.clash.pool_kernels": {candidate.proxy_id: "native"},
                "proxy.clash.selected_proxy_id": candidate.proxy_id,
            }
        )
        bridge = _PoolBridge()
        with (
            patch.object(admin_proxy, "config", fake_config),
            patch.object(admin_proxy, "get_proxy_bridge_manager", return_value=bridge),
            patch.object(
                admin_proxy,
                "speedtest_config",
                return_value=("https://example.com", 2.0, False, 1),
            ),
            patch.object(admin_proxy, "iter_clash_candidate_results", fake_results),
        ):
            response = await admin_proxy.stream_clash_test(
                admin_proxy.ClashSpeedTestRequest(formal_pool=True)
            )
            async for _chunk in response.body_iterator:
                pass

        self.assertEqual(fake_config.values["proxy.clash.pool_proxy_ids"], [])
        self.assertFalse(fake_config.values["proxy.clash.enabled"])
        self.assertEqual(fake_config.values["proxy.clash.selected_proxy_id"], "")
        self.assertEqual(fake_config.values["proxy.clash.selected_proxy_name"], "")
        self.assertEqual(fake_config.values["proxy.clash.selected_kernel"], "auto")
        self.assertEqual(bridge.stopped_candidates, [candidate.proxy_id])


class ClashSpeedTestTests(unittest.IsolatedAsyncioTestCase):
    def test_kernel_auto_download_is_enabled_by_default(self):
        with Path("config.defaults.toml").open("rb") as config_file:
            defaults = tomllib.load(config_file)

        self.assertTrue(defaults["proxy"]["kernels"]["auto_download"])

        with patch(
            "app.control.proxy.speedtest.get_config",
            return_value=_FakeProxyConfig(),
        ):
            _, _, auto_download, _ = speedtest_config()
        self.assertTrue(auto_download)

    async def test_native_candidate_reports_alive_latency(self):
        candidate = parse_clash_yaml(ClashProxyParserTests.sample_yaml)[0]

        class _FakeBridge:
            def __init__(self):
                self.url = None
                self.preferred = None

            async def ensure_candidate(self, candidate, preferred, *, auto_download):
                self.url = candidate.proxy_url
                self.preferred = preferred
                return candidate.proxy_url, "native"

            def stop_candidate(self, candidate, kernel=None):
                return None

        class _FakeResponse:
            status_code = 204
            def json(self):
                return {'ip': '203.0.113.7'}

        class _FakeSession:
            last_kwargs = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                type(self).last_kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def get(self, url, *, timeout, allow_redirects):
                self.url = url
                self.timeout = timeout
                self.allow_redirects = allow_redirects
                return _FakeResponse()

        bridge = _FakeBridge()
        with patch("curl_cffi.requests.AsyncSession", _FakeSession):
            result = await run_clash_candidate(
                candidate,
                bridge,
                target_url="https://example.com/health",
                timeout_sec=2,
                preferred_kernel="xray",
            )

        self.assertEqual(result["state"], "alive")
        self.assertEqual(result["status_code"], 204)
        self.assertEqual(result["egress_ip"], "203.0.113.7")
        self.assertIsInstance(result["latency_ms"], int)
        self.assertEqual(_FakeSession.last_kwargs["proxies"]["https"], candidate.proxy_url)
        self.assertEqual(bridge.preferred, "native")

    async def test_candidate_results_are_yielded_as_each_task_finishes(self):
        candidates = parse_clash_yaml(ClashProxyParserTests.sample_yaml)[:2]

        async def fake_test(candidate, bridge_manager, **kwargs):
            if candidate is candidates[0]:
                await asyncio.sleep(0.02)
            return {"state": "alive", "latency_ms": 1}

        with patch("app.control.proxy.speedtest.test_clash_candidate", fake_test):
            order = []
            async for proxy_id, _ in iter_clash_candidate_results(
                candidates,
                _FakeAdminBridge(),
                concurrency=2,
            ):
                order.append(proxy_id)

        self.assertEqual(order, [candidates[1].proxy_id, candidates[0].proxy_id])

    async def test_kernel_download_failure_is_reported_as_unavailable(self):
        candidate = parse_clash_yaml(ClashProxyParserTests.sample_yaml)[2]

        class _FailingBridge:
            async def ensure_candidate(self, candidate, preferred, *, auto_download):
                raise KernelBridgeError("Xray 内核下载失败")

            def stop_candidate(self, candidate, kernel=None):
                return None

        result = await run_clash_candidate(
            candidate,
            _FailingBridge(),
            target_url="https://example.com/health",
            timeout_sec=2,
            preferred_kernel="xray",
            auto_download=True,
        )

        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["error"], "Xray 内核下载失败")


class ClashProxyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_proxy_override_is_scoped_to_one_lease(self):
        fake_config = _FakeProxyConfig()
        directory = ProxyDirectory()
        with patch("app.control.proxy.get_config", return_value=fake_config):
            await directory.load()
            selected = await directory.acquire(
                proxy_url_override="http://selected.example.com:8081"
            )
            global_lease = await directory.acquire()

        self.assertEqual(selected.proxy_url, "http://selected.example.com:8081")
        self.assertTrue(selected.proxy_override)
        self.assertEqual(global_lease.proxy_url, "socks5://127.0.0.1:7891")
        self.assertFalse(global_lease.proxy_override)

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

    async def test_clash_pool_loads_multiple_nodes_into_proxy_pool(self):
        candidates = parse_clash_yaml(ClashProxyParserTests.sample_yaml)
        fake_config = _FakeProxyConfig()
        fake_config.values.update(
            {
                "proxy.egress.mode": "direct",
                "proxy.egress.proxy_url": "",
                "proxy.clash.pool_proxy_ids": [
                    candidates[0].proxy_id,
                    candidates[1].proxy_id,
                ],
                "proxy.clash.pool_kernels": {
                    candidates[0].proxy_id: "native",
                    candidates[1].proxy_id: "native",
                },
            }
        )

        class _FakePoolBridge:
            def __init__(self):
                self.calls = []

            async def ensure_candidate(self, candidate, preferred, *, auto_download):
                self.calls.append((candidate.name, preferred))
                return f"socks5://127.0.0.1:{19000 + len(self.calls)}", preferred

        bridge = _FakePoolBridge()
        directory = ProxyDirectory()
        with (
            patch("app.control.proxy.get_config", return_value=fake_config),
            patch("app.control.proxy.get_proxy_bridge_manager", return_value=bridge),
        ):
            await directory.load()

        self.assertEqual(directory.egress_mode.value, "proxy_pool")
        self.assertEqual(
            [node.proxy_url for node in directory.nodes],
            ["socks5://127.0.0.1:19001", "socks5://127.0.0.1:19002"],
        )
        self.assertEqual(bridge.calls, [("HTTP 节点", "native"), ("SOCKS 节点", "native")])


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

    def test_free_port_checks_tcp_and_udp_together(self):
        allocated_ports = iter((56892, 57042))
        sockets = []

        class _FakeSocket:
            def __init__(self, _family, kind):
                self.kind = kind
                self.port = None
                sockets.append(self)

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                self.close()

            def setsockopt(self, _level, _option, _value):
                return None

            def bind(self, address):
                requested_port = address[1]
                if requested_port == 0:
                    self.port = next(allocated_ports)
                elif self.kind == socket.SOCK_DGRAM and requested_port == 56892:
                    raise PermissionError("excluded UDP port")
                else:
                    self.port = requested_port

            def getsockname(self):
                return ("127.0.0.1", self.port)

            def close(self):
                return None

        with patch("app.control.proxy.bridge.socket.socket", _FakeSocket):
            port = ProxyKernelBridgeManager._free_port()

        self.assertEqual(port, 57042)
        self.assertEqual(
            [sock.kind for sock in sockets],
            [socket.SOCK_STREAM, socket.SOCK_DGRAM] * 2,
        )

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

    def test_release_asset_selection_matches_windows_architecture(self):
        spec = KernelSpec("xray", "Xray", "XTLS/Xray-core", "xray", "XRAY_BINARY_PATH")
        assets = [
            {"name": "Xray-windows-64.zip", "browser_download_url": "amd64", "size": 1},
            {"name": "Xray-windows-arm64-v8a.zip", "browser_download_url": "arm64", "size": 1},
        ]

        selected = ProxyKernelManager._select_asset(spec, assets, "windows", "amd64")
        self.assertEqual(selected.url, "amd64")


if __name__ == "__main__":
    unittest.main()
