from dataclasses import replace
import unittest
from unittest.mock import patch

from app.control.model import aliases
from app.control.model.cooldown import mark_model_success, mark_rate_limited, reset_rate_limits
from app.control.model import registry


class ModelAliasesTests(unittest.TestCase):
    def tearDown(self):
        aliases.reset_runtime_state()
        reset_rate_limits()

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

    def test_free_alias_ignores_non_console_and_unsupported_candidates(self):
        with patch.object(
            aliases,
            "get_config",
            return_value={
                "FREE": [
                    "grok-composer-2.5-fast",
                    "grok-4.20-fast",
                ]
            },
        ):
            resolved = aliases.resolve("FREE")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.model, "grok-4.3-console")
        self.assertTrue(resolved.spec.is_console_chat())
        self.assertTrue(resolved.spec.supported_in_api)

    def test_free_alias_keeps_only_supported_console_fallbacks(self):
        with patch.object(
            aliases,
            "get_config",
            return_value={
                "FREE": {
                    "stable": ["grok-4.20-fast", "grok-4.3-console"],
                    "degraded": ["grok-composer-2.5-fast", "grok-4.20-0309-console"],
                }
            },
        ):
            resolved = aliases.resolve("FREE")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.model, "grok-4.3-console")
        self.assertEqual(
            aliases.fallback_candidates(resolved),
            ("grok-4.20-0309-console",),
        )

    def test_alias_supported_in_api_reflects_candidate_contract(self):
        with patch.object(
            aliases,
            "get_config",
            return_value={
                "FREE": ["grok-4.3-console"],
                "CUSTOM": ["grok-composer-2.5-fast"],
            },
        ):
            self.assertTrue(aliases.alias_supported_in_api("FREE"))
            self.assertFalse(aliases.alias_supported_in_api("CUSTOM"))
            self.assertIsNone(aliases.alias_supported_in_api("grok-4.3-console"))

    def test_resolution_contract_rejects_incompatible_virtual_candidate(self):
        with patch.object(
            aliases,
            "get_config",
            return_value={"FREE": ["grok-4.3-console"]},
        ):
            resolved = aliases.resolve("FREE")

        self.assertIsNotNone(resolved)
        self.assertTrue(aliases.is_resolution_usable(resolved))
        incompatible = replace(
            resolved,
            model="grok-4.20-fast",
            spec=registry.resolve("grok-4.20-fast"),
        )
        self.assertFalse(aliases.is_resolution_usable(incompatible))

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

    def test_empty_virtual_mapping_falls_back_to_default_candidate(self):
        with patch.object(aliases, "get_config", return_value={"FREE": []}):
            resolved = aliases.resolve("FREE")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.model, "grok-4.3-console")
        self.assertTrue(resolved.is_virtual)

    def test_missing_virtual_mapping_falls_back_to_default_aliases(self):
        with patch.object(aliases, "get_config", return_value={}):
            free = aliases.resolve("FREE")
            super_resolved = aliases.resolve("SUPER")

        self.assertIsNotNone(free)
        self.assertEqual(free.model, "grok-4.3-console")
        self.assertIsNotNone(super_resolved)
        self.assertEqual(super_resolved.model, "grok-4.20-auto")

    def test_stable_pool_round_robins_and_degraded_pool_is_weighted(self):
        config = {
            "FREE": {
                "stable": ["grok-4.3-console", "grok-4.3-low"],
                "degraded": ["grok-4.3-medium"],
            }
        }
        with patch.object(aliases, "get_config", return_value=config):
            first = [aliases.resolve("FREE") for _ in range(4)]
            self.assertEqual(
                [item.model for item in first],
                ["grok-4.3-console", "grok-4.3-low", "grok-4.3-console", "grok-4.3-low"],
            )

            batch = [aliases.resolve("FREE") for _ in range(20)]

        self.assertEqual(sum(item.pool == "degraded" for item in batch), 1)
        degraded_index = next(index for index, item in enumerate(batch) if item.pool == "degraded")
        self.assertEqual(batch[degraded_index].model, "grok-4.3-medium")

    def test_successful_degraded_probe_is_promoted_to_stable(self):
        config = {
            "FREE": {
                "stable": ["grok-4.3-console"],
                "degraded": ["grok-4.3-medium"],
            }
        }
        with patch.object(aliases, "get_config", return_value=config):
            for _ in range(19):
                aliases.resolve("FREE")
            probe = aliases.resolve("FREE")
            self.assertEqual(probe.pool, "degraded")

            mark_model_success(probe.model)
            first_after_promotion = aliases.resolve("FREE")
            second_after_promotion = aliases.resolve("FREE")

        self.assertEqual(first_after_promotion.pool, "stable")
        self.assertEqual(second_after_promotion.model, "grok-4.3-medium")
        self.assertEqual(second_after_promotion.pool, "stable")

    def test_rate_limited_promoted_model_returns_to_degraded_pool(self):
        config = {
            "FREE": {
                "stable": ["grok-4.3-console"],
                "degraded": ["grok-4.3-medium"],
            }
        }
        with patch.object(aliases, "get_config", return_value=config):
            for _ in range(19):
                aliases.resolve("FREE")
            probe = aliases.resolve("FREE")
            mark_model_success(probe.model)
            mark_rate_limited(probe.model, 0)

            for _ in range(19):
                aliases.resolve("FREE")
            recovered_probe = aliases.resolve("FREE")

        self.assertEqual(recovered_probe.model, "grok-4.3-medium")
        self.assertEqual(recovered_probe.pool, "degraded")

    def test_successful_probe_restores_rate_limited_stable_model(self):
        config = {
            "FREE": {
                "stable": ["grok-4.3-console"],
                "degraded": ["grok-4.3-medium"],
            }
        }
        with patch.object(aliases, "get_config", return_value=config):
            mark_rate_limited("grok-4.3-console", 0)
            probe = aliases.resolve("FREE")
            self.assertEqual(probe.pool, "degraded")

            mark_model_success("grok-4.3-console")
            restored = aliases.resolve("FREE")

        self.assertEqual(restored.model, "grok-4.3-console")
        self.assertEqual(restored.pool, "stable")

    def test_routing_snapshot_ignores_non_advancing_resolution(self):
        config = {
            "FREE": {
                "stable": ["grok-4.3-console"],
                "degraded": ["grok-4.3-medium"],
            }
        }
        with patch.object(aliases, "get_config", return_value=config):
            aliases.resolve("FREE", advance=False)
            before = aliases.routing_snapshot()
            aliases.resolve("FREE")
            after = aliases.routing_snapshot()

        self.assertEqual(before["aliases"][0]["stats"]["total"], 0)
        self.assertEqual(after["aliases"][0]["stats"]["total"], 1)
        self.assertEqual(after["aliases"][0]["stats"]["stable"], 1)
        self.assertEqual(after["aliases"][0]["stats"]["degraded"], 0)
        self.assertEqual(after["aliases"][0]["models"][0]["requests"], 1)
        self.assertEqual(len(after["aliases"][0]["recent"]), 1)

    def test_routing_snapshot_tracks_nineteen_to_one_pool_schedule(self):
        config = {
            "FREE": {
                "stable": ["grok-4.3-console"],
                "degraded": ["grok-4.3-medium"],
            }
        }
        with patch.object(aliases, "get_config", return_value=config):
            for _ in range(20):
                aliases.resolve("FREE")
            stats = aliases.routing_snapshot()["aliases"][0]

        self.assertEqual(stats["stats"]["total"], 20)
        self.assertEqual(stats["stats"]["stable"], 19)
        self.assertEqual(stats["stats"]["degraded"], 1)
        self.assertEqual(stats["models"][0]["requests"], 19)
        self.assertEqual(stats["models"][1]["requests"], 1)
        self.assertEqual(len(stats["recent"]), 20)

    def test_routing_snapshot_tracks_pool_events_latest_fifty(self):
        config = {
            "FREE": {
                "stable": ["grok-4.3-console"],
                "degraded": ["grok-4.3-medium"],
            }
        }
        with patch.object(aliases, "get_config", return_value=config):
            for _ in range(55):
                aliases.demote_model("grok-4.3-console")
                aliases.promote_model("grok-4.3-console")
            stats = aliases.routing_snapshot()["aliases"][0]

        self.assertEqual(len(stats["pool_events"]), 50)
        self.assertEqual(stats["pool_events"][0]["sequence"], 110)
        self.assertEqual(stats["pool_events"][-1]["sequence"], 61)
        self.assertEqual(stats["pool_events"][0]["action"], "promote")
        self.assertEqual(stats["pool_events"][1]["action"], "demote")
        self.assertEqual(stats["pool_events"][0]["from_pool"], "degraded")
        self.assertEqual(stats["pool_events"][0]["to_pool"], "stable")

    def test_configured_default_models_exist(self):
        self.assertIsNotNone(registry.get("grok-4.3"))
        self.assertIsNotNone(registry.get("grok-4.3-console"))
        self.assertIsNotNone(registry.get("grok-4.5"))
        self.assertIsNotNone(registry.get("grok-4.5-console"))
        self.assertIsNotNone(registry.get("grok-4.5-high"))
        self.assertIsNotNone(registry.get("grok-4.6"))
        self.assertIsNotNone(registry.get("grok-4.6-xhigh"))
        self.assertIsNotNone(registry.get("grok-4.20-0309-console"))
        self.assertIsNotNone(registry.get("grok-4.20-auto"))
        self.assertIsNotNone(registry.get("grok-4.3-beta"))
        self.assertIsNotNone(registry.get("grok-build-0.1"))


if __name__ == "__main__":
    unittest.main()
