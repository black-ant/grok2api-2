import unittest

from app.control.proxy.models import ProxyLease
from app.control.model import registry
from app.dataplane.proxy.adapters.headers import build_console_headers
from app.dataplane.reverse.protocol.xai_console_chat import build_console_payload
from app.products.openai.router import _codex_model_catalog


class ConsoleModelPayloadTests(unittest.TestCase):
    def test_console_headers_keep_full_clearance_cookie_bundle(self):
        lease = ProxyLease(
            lease_id="lease-console",
            cf_cookies="__cf_bm=bm-token; cf_clearance=clearance-token",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136.0.0.0",
        )

        cookie = build_console_headers("sso-token", lease=lease)["Cookie"]

        self.assertEqual(
            cookie,
            "sso=sso-token; sso-rw=sso-token; __cf_bm=bm-token; "
            "cf_clearance=clearance-token",
        )

    def test_remote_media_and_audio_models_are_cataloged(self):
        expected = {
            "grok-imagine-image-quality": (True, True),
            "grok-imagine-image-2.0": (True, True),
            "grok-imagine-image-quality-2.0": (True, True),
            "grok-imagine-video-1.5": (True, True),
            "grok-voice-latest": (True, True),
            "grok-voice-think-fast-2.0": (True, True),
            "grok-voice-think-fast-1.0": (True, True),
            "grok-stt": (True, True),
        }
        for model, (is_cataloged, supported_in_api) in expected.items():
            spec = registry.get(model)
            self.assertEqual(spec is not None, is_cataloged, model)
            self.assertEqual(spec.supported_in_api, supported_in_api, model)

        self.assertTrue(registry.resolve("grok-voice-latest").is_tts())
        self.assertTrue(registry.resolve("grok-voice-latest").is_realtime())
        self.assertTrue(registry.resolve("grok-stt").is_stt())

    def test_remote_catalog_base_models_are_console_models(self):
        for model in ("grok-4.3", "grok-build-0.1"):
            spec = registry.get(model)
            self.assertIsNotNone(spec)
            self.assertTrue(spec.is_console_chat())

    def test_grok_46_models_are_console_models(self):
        for model in (
            "grok-4.6",
            "grok-4.6-low",
            "grok-4.6-medium",
            "grok-4.6-high",
            "grok-4.6-xhigh",
        ):
            spec = registry.get(model)
            self.assertIsNotNone(spec)
            self.assertTrue(spec.is_console_chat())

    def test_grok_45_models_are_console_models(self):
        for model in (
            "grok-4.5",
            "grok-4.5-console",
            "grok-4.5-low",
            "grok-4.5-medium",
            "grok-4.5-high",
        ):
            spec = registry.get(model)
            self.assertIsNotNone(spec)
            self.assertTrue(spec.is_console_chat())

    def test_grok_45_high_alias_sets_fixed_effort(self):
        payload = build_console_payload(
            messages=[{"role": "user", "content": "hello"}],
            model="grok-4.5-high",
            reasoning_effort="low",
        )

        self.assertEqual(payload["model"], "grok-4.5")
        self.assertEqual(payload["reasoning"], {"effort": "high"})

    def test_codex_catalog_reports_grok_45_capabilities(self):
        catalog = _codex_model_catalog([
            ("grok-4.5", registry.resolve("grok-4.5"), "Grok 4.5 (Console)"),
        ])

        model = catalog["models"][0]
        self.assertEqual(model["context_window"], 500_000)
        self.assertEqual(model["max_context_window"], 500_000)
        self.assertEqual(model["default_reasoning_level"], "low")
        self.assertEqual(
            [item["effort"] for item in model["supported_reasoning_levels"]],
            ["low", "medium", "high"],
        )

    def test_grok_46_xhigh_alias_sets_fixed_effort(self):
        payload = build_console_payload(
            messages=[{"role": "user", "content": "hello"}],
            model="grok-4.6-xhigh",
            reasoning_effort="low",
        )

        self.assertEqual(payload["model"], "grok-4.6")
        self.assertEqual(payload["reasoning"], {"effort": "xhigh"})

    def test_grok_46_max_effort_maps_to_xhigh(self):
        payload = build_console_payload(
            messages=[{"role": "user", "content": "hello"}],
            model="grok-4.6",
            reasoning_effort="max",
        )

        self.assertEqual(payload["reasoning"], {"effort": "xhigh"})

    def test_grok_build_base_model_keeps_fixed_no_reasoning_contract(self):
        payload = build_console_payload(
            messages=[{"role": "user", "content": "hello"}],
            model="grok-build-0.1",
            reasoning_effort="high",
        )

        self.assertEqual(payload["model"], "grok-build-0.1")
        self.assertEqual(payload["max_output_tokens"], 256_000)
        self.assertNotIn("reasoning", payload)

    def test_codex_catalog_reports_build_as_no_reasoning(self):
        catalog = _codex_model_catalog([
            ("grok-build-0.1", registry.resolve("grok-build-0.1"), "Grok Build 0.1 (Console)"),
        ])

        model = catalog["models"][0]
        self.assertEqual(model["context_window"], 256_000)
        self.assertEqual(model["default_reasoning_level"], "none")
        self.assertEqual(
            [item["effort"] for item in model["supported_reasoning_levels"]],
            ["none"],
        )

    def test_legacy_model_max_effort_remains_high(self):
        payload = build_console_payload(
            messages=[{"role": "user", "content": "hello"}],
            model="grok-4.3-console",
            reasoning_effort="max",
        )

        self.assertEqual(payload["reasoning"], {"effort": "high"})


if __name__ == "__main__":
    unittest.main()
