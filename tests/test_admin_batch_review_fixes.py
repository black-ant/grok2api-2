import asyncio
from copy import deepcopy
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import orjson

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.control.account.refresh import RefreshResult
from app.control.account.backends.redis import RedisAccountRepository
from app.platform.errors import ValidationError
from app.products.web.admin.batch import BatchRequest, batch_refresh
from app.products.web.admin import (
    ModelMappingAppendRequest,
    _append_model_to_alias,
    append_model_mapping,
    tokens as admin_tokens,
)


class _Repo:
    def __init__(self) -> None:
        self.records = {
            "active-token": AccountRecord(token="active-token", status=AccountStatus.ACTIVE),
            "disabled-token": AccountRecord(token="disabled-token", status=AccountStatus.DISABLED),
        }
        self.requested_tokens: list[str] = []

    async def get_accounts(self, tokens: list[str]) -> list[AccountRecord]:
        self.requested_tokens = tokens
        return [self.records[token] for token in tokens if token in self.records]


class _RefreshService:
    def __init__(self) -> None:
        self.refreshed_tokens: list[str] = []

    async def refresh_tokens(self, tokens: list[str]) -> RefreshResult:
        self.refreshed_tokens.extend(tokens)
        return RefreshResult(refreshed=len(tokens))


class _Pipeline:
    def __init__(self, redis: "_Redis") -> None:
        self.redis = redis
        self.keys: list[str] = []

    async def __aenter__(self) -> "_Pipeline":
        self.redis.pipeline_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def hgetall(self, key: str) -> None:
        self.keys.append(key)

    async def execute(self) -> list[dict[str, str]]:
        return [self.redis.hashes.get(key, {}) for key in self.keys]


class _Redis:
    def __init__(self) -> None:
        active = AccountRecord(token="active-token", status=AccountStatus.ACTIVE)
        self.hashes = {
            "accounts:record:active-token": RedisAccountRepository._to_hash(active, revision=7),
        }
        self.pipeline_count = 0
        self.hgetall_count = 0

    def pipeline(self) -> _Pipeline:
        return _Pipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        self.hgetall_count += 1
        return self.hashes.get(key, {})


class AdminBatchReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_refresh_filters_non_manageable_explicit_tokens(self):
        repo = _Repo()
        refresh_svc = _RefreshService()

        response = await batch_refresh(
            BatchRequest(tokens=["active-token", "disabled-token"]),
            async_mode=False,
            all_manageable=False,
            concurrency=None,
            repo=repo,
            refresh_svc=refresh_svc,
        )

        body = orjson.loads(response.body)
        self.assertEqual(repo.requested_tokens, ["active-token", "disabled-token"])
        self.assertEqual(refresh_svc.refreshed_tokens, ["active-token"])
        self.assertEqual(
            body["summary"],
            {"total": 1, "ok": 1, "fail": 0, "expired": 0, "transient": 0},
        )

    async def test_batch_refresh_rejects_only_non_manageable_explicit_tokens(self):
        repo = _Repo()
        refresh_svc = _RefreshService()

        with self.assertRaises(ValidationError) as cm:
            await batch_refresh(
                BatchRequest(tokens=["disabled-token"]),
                async_mode=False,
                all_manageable=False,
                concurrency=None,
                repo=repo,
                refresh_svc=refresh_svc,
            )

        self.assertIn("No manageable tokens available", str(cm.exception))
        self.assertEqual(refresh_svc.refreshed_tokens, [])

    def test_model_mapping_append_keeps_existing_candidates(self):
        aliases = {
            "FREE": {
                "stable": ["grok-4.3-console"],
                "degraded": ["grok-4.20-0309-console"],
                "stable_ratio": 95,
                "degraded_ratio": 5,
            }
        }

        self.assertTrue(_append_model_to_alias(aliases, "FREE", "grok-4.3-low"))
        self.assertTrue(
            _append_model_to_alias(aliases, "FREE", "grok-4.3-medium")
        )
        self.assertEqual(
            aliases["FREE"]["stable"],
            ["grok-4.3-console", "grok-4.3-low", "grok-4.3-medium"],
        )

    async def test_model_mapping_append_keeps_candidates_across_requests(self):
        state = {
            "FREE": {
                "stable": ["grok-4.3-console"],
                "degraded": [],
                "stable_ratio": 95,
                "degraded_ratio": 5,
            },
            "SUPER": {
                "stable": ["grok-4.20-auto"],
                "degraded": [],
                "stable_ratio": 95,
                "degraded_ratio": 5,
            },
        }

        async def apply_update(patch):
            state.clear()
            state.update(deepcopy(patch["models"]["aliases"]))

        fake_config = SimpleNamespace(
            load=AsyncMock(),
            update=AsyncMock(side_effect=apply_update),
        )

        with (
            patch("app.products.web.admin.config", fake_config),
            patch(
                "app.products.web.admin.model_aliases.alias_config_map",
                side_effect=lambda: deepcopy(state),
            ),
            patch(
                "app.products.web.admin.model_aliases.normalize_alias_config",
                side_effect=lambda value: deepcopy(value),
            ),
            patch("app.products.web.admin.model_aliases.reset_runtime_state"),
        ):
            first = await append_model_mapping(
                ModelMappingAppendRequest(alias="FREE", model="first-model")
            )
            second = await append_model_mapping(
                ModelMappingAppendRequest(alias="FREE", model="second-model")
            )

        self.assertTrue(first["added"])
        self.assertTrue(second["added"])
        self.assertRegex(first["mutation_id"], r"^[0-9a-f]{12}$")
        self.assertRegex(second["mutation_id"], r"^[0-9a-f]{12}$")
        self.assertEqual(
            second["data"]["aliases"]["FREE"]["stable"],
            ["grok-4.3-console", "first-model", "second-model"],
        )


class RedisRepositoryReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_accounts_reads_many_tokens_with_one_pipeline(self):
        redis = _Redis()
        repo = RedisAccountRepository(redis)

        records = await repo.get_accounts(["active-token", "missing-token"])

        self.assertEqual([record.token for record in records], ["active-token"])
        self.assertEqual(redis.pipeline_count, 1)
        self.assertEqual(redis.hgetall_count, 0)


class AccountHtmlReviewFixTests(unittest.TestCase):
    def test_disabled_nsfw_buttons_use_row_specific_unavailable_reason(self):
        with open("app/statics/admin/account.html", encoding="utf-8") as fh:
            html = fh.read()
        disabled_branches = re.findall(
            r"data-tip=\"\$\{xe\(canManageNsfw \? tr\('account\.batchNsfw(?:Disable)?'.*?"
            r": tr\('account\.rowActionNotSupported'.*?aria-label=\"\$\{xe\((.*?)\)\}\"",
            html,
        )

        self.assertEqual(len(disabled_branches), 2)
        self.assertTrue(
            all(
                "canManageNsfw ?" in branch and "account.rowActionNotSupported" in branch
                for branch in disabled_branches
            )
        )

    def test_row_action_not_supported_is_translated_for_all_account_locales(self):
        for path in Path("app/statics/i18n").glob("*.json"):
            data = orjson.loads(path.read_bytes())
            with self.subTest(locale=path.name):
                self.assertIn("account", data, f"Locale {path.name} missing account section")
                self.assertIn("rowActionNotSupported", data["account"])


class ConfigHtmlReviewFixTests(unittest.TestCase):
    def test_get_current_value_preserves_schema_defaults(self):
        html = Path("app/statics/admin/config.html").read_text(encoding="utf-8")

        self.assertIn("function _getCurrentValue(section, key, field)", html)
        self.assertIn("_getValue(section, key, field)", html)
        self.assertIn("_getCurrentValue(section, field.key, field)", html)


class GrokCapabilityHtmlReviewFixTests(unittest.TestCase):
    def test_batch_alias_add_reports_pending_and_failure(self):
        html = Path('app/statics/admin/grok-capability.html').read_text(encoding='utf-8')

        self.assertIn('aliasMutationErrors.delete(key);', html)
        self.assertIn('aliasMutationErrors.set(key, message);', html)
        self.assertIn('result-action-feedback', html)

    def test_batch_model_list_keeps_visible_height(self):
        html = Path("app/statics/admin/grok-capability.html").read_text(encoding="utf-8")

        self.assertIn(".batch-list { flex:0 0 auto; min-height:120px; max-height:52vh;", html)
        self.assertIn(".batch-modal { width:min(880px,94vw); max-height:calc(100dvh - 32px); display:flex; flex-direction:column; overflow-y:auto }", html)
        self.assertIn(".batch-results { margin-top:12px; flex:0 0 auto; min-height:0; max-height:52vh; overflow:auto }", html)
        self.assertIn("function renderBatchModels", html)
        self.assertIn("id=\"batch-model-list\"", html)

    def test_batch_alias_adds_are_serialized(self):
        html = Path("app/statics/admin/grok-capability.html").read_text(encoding="utf-8")

        self.assertIn("let aliasMutationQueue = Promise.resolve();", html)
        self.assertIn("const pendingAliasAdds = new Set();", html)
        self.assertIn(
            "async function refreshAliasCache() {\n"
            "  const operation = aliasMutationQueue.then(async () => {",
            html,
        )
        self.assertIn("apiFetch('/model-mapping/append'", html)
        self.assertIn("body: JSON.stringify({ alias: aliasName, model: modelId })", html)
        self.assertIn("if (!aliasHasModel(aliasName, modelId))", html)
        self.assertIn("const operation = aliasMutationQueue.then(async () =>", html)
        self.assertIn("aliasMutationQueue = operation.catch(() => {});", html)
        self.assertIn("加入中…", html)

    def test_batch_modal_defaults_to_all_models(self):
        html = Path("app/statics/admin/grok-capability.html").read_text(encoding="utf-8")

        self.assertIn(
            "function openBatchModal() {\n"
            "  batchSelection = new Set(models.map((item) => item.id));",
            html,
        )

    def test_capability_scan_can_use_actual_routing_pool(self):
        html = Path("app/statics/admin/grok-capability.html").read_text(encoding="utf-8")

        self.assertIn('value="routing" selected', html)
        self.assertIn("selection_mode: selectionMode", html)
        self.assertIn("实际发送池 · 按模型自动选择 Key", html)
        self.assertIn("实际发送池 · ${item.account_pool", html)
        report = Path("app/statics/admin/grok-capability-report.html").read_text(encoding="utf-8")
        self.assertIn("report.selection_mode === 'routing'", report)
        self.assertIn("实际发送池 · ${item.account_pool", report)


class AdminTokenTaskReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        getattr(admin_tokens, "_background_tasks", set()).clear()

    async def asyncTearDown(self) -> None:
        pending = list(getattr(admin_tokens, "_background_tasks", set()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        getattr(admin_tokens, "_background_tasks", set()).clear()

    async def test_fire_and_forget_keeps_task_until_completion(self):
        release = asyncio.Event()

        async def _wait() -> None:
            await release.wait()

        task = admin_tokens._fire_and_forget(_wait())

        self.assertIn(task, admin_tokens._background_tasks)
        release.set()
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, admin_tokens._background_tasks)


if __name__ == "__main__":
    unittest.main()
