import unittest
from unittest.mock import patch

from app.control.model import aliases
from app.control.model import registry


class ModelAliasesTests(unittest.TestCase):
    def test_resolves_virtual_model_to_first_enabled_candidate(self):
        with patch.object(
            aliases,
            "get_config",
            return_value={"FREE": ["missing-model", "grok-4.3-console"]},
        ):
            resolved = aliases.resolve("FREE")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.requested_model, "FREE")
        self.assertEqual(resolved.model, "grok-4.3-console")
        self.assertTrue(resolved.is_virtual)

    def test_resolves_virtual_model_by_available_pool(self):
        def is_available(spec, pools):
            return spec.model_name == "grok-4.3-beta" and "super" in pools

        with patch.object(
            aliases,
            "get_config",
            return_value={"SUPER": ["grok-4.20-auto", "grok-4.3-beta"]},
        ):
            resolved = aliases.resolve(
                "SUPER",
                available_pools=frozenset({"super"}),
                is_available=is_available,
            )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.model, "grok-4.3-beta")

    def test_resolves_virtual_model_around_temporarily_blocked_candidate(self):
        with patch.object(
            aliases,
            "get_config",
            return_value={
                "FREE": ["grok-4.3-console", "grok-4.20-0309-console"],
            },
        ):
            resolved = aliases.resolve(
                "FREE",
                blocked_model_names=frozenset({"grok-4.3-console"}),
            )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.model, "grok-4.20-0309-console")

    def test_virtual_model_fallback_candidates_follow_mapping_order(self):
        with patch.object(
            aliases,
            "get_config",
            return_value={
                "FREE": [
                    "grok-4.3-console",
                    "grok-4.20-0309-console",
                    "grok-4.20-auto",
                ],
            },
        ):
            resolved = aliases.resolve("FREE")

        self.assertIsNotNone(resolved)
        self.assertEqual(
            aliases.fallback_candidates(resolved),
            ("grok-4.20-0309-console",),
        )

    def test_real_model_still_resolves_for_backward_compatibility(self):
        with patch.object(aliases, "get_config", return_value={}):
            resolved = aliases.resolve("grok-4.20-auto")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.model, "grok-4.20-auto")
        self.assertFalse(resolved.is_virtual)

    def test_empty_virtual_mapping_is_not_resolved(self):
        with patch.object(aliases, "get_config", return_value={"FREE": []}):
            resolved = aliases.resolve("FREE")

        self.assertIsNone(resolved)

    def test_configured_default_models_exist(self):
        self.assertIsNotNone(registry.get("grok-4.3-console"))
        self.assertIsNotNone(registry.get("grok-4.20-0309-console"))
        self.assertIsNotNone(registry.get("grok-4.20-auto"))
        self.assertIsNotNone(registry.get("grok-4.3-beta"))


if __name__ == "__main__":
    unittest.main()
