import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.products.anthropic.router import MessagesRequest, messages_endpoint
from app.products.openai.router import ChatCompletionRequest, list_models, chat_completions_endpoint


class _RequestState(SimpleNamespace):
    pass


class _Request(SimpleNamespace):
    pass


class _DummyConfig:
    def get_bool(self, _key, default=False):
        return default


class VirtualModelHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_models_endpoint_exposes_virtual_models(self):
        request = _Request(app=SimpleNamespace(state=SimpleNamespace(repository=None)))

        async def fake_available_pools(_request):
            return frozenset({"basic", "super"})

        with patch("app.control.model.aliases.get_config", return_value={
            "FREE": ["grok-4.3-console", "grok-4.20-0309-console"],
            "SUPER": ["grok-4.20-auto", "grok-4.3-beta"],
        }), patch("app.products.openai.router._available_pools", new=fake_available_pools):
            response = await list_models(request)

        payload = response.body.decode("utf-8")
        self.assertIn('"FREE"', payload)
        self.assertIn('"SUPER"', payload)
        self.assertIn('"grok-4.3-console"', payload)
        self.assertIn('"grok-4.20-auto"', payload)

    async def test_chat_completion_free_maps_to_real_model(self):
        request = _Request(app=SimpleNamespace(state=SimpleNamespace(repository=None)), state=_RequestState())
        captured = {}

        async def fake_available_pools(_request):
            return frozenset({"basic"})

        async def fake_chat_completions(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "model": kwargs["model"]}

        with patch("app.control.model.aliases.get_config", return_value={
            "FREE": ["grok-4.3-console"],
            "SUPER": ["grok-4.20-auto"],
        }), patch("app.platform.config.snapshot.get_config", return_value=_DummyConfig()), patch(
            "app.products.openai.router._available_pools", new=fake_available_pools
        ), patch(
            "app.products.openai.router.chat_completions",
            new=AsyncMock(side_effect=fake_chat_completions),
        ):
            response = await chat_completions_endpoint(
                ChatCompletionRequest(
                    model="FREE",
                    messages=[{"role": "user", "content": "hi"}],
                ),
                request,
            )

        self.assertEqual(response.body.decode("utf-8").find("grok-4.3-console") >= 0, True)
        self.assertEqual(captured["model"], "grok-4.3-console")
        self.assertEqual(request.state.request_log_routing["resolved_model"], "grok-4.3-console")

    async def test_anthropic_messages_free_maps_to_real_model(self):
        request = _Request(app=SimpleNamespace(state=SimpleNamespace(repository=None)), state=_RequestState())
        captured = {}

        async def fake_available_pools(_request):
            return frozenset({"basic"})

        async def fake_messages_create(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "model": kwargs["model"]}

        with patch("app.control.model.aliases.get_config", return_value={
            "FREE": ["grok-4.3-console"],
            "SUPER": ["grok-4.20-auto"],
        }), patch("app.platform.config.snapshot.get_config", return_value=_DummyConfig()), patch(
            "app.products.anthropic.router._available_pools", new=fake_available_pools
        ), patch(
            "app.products.anthropic.messages.create",
            new=AsyncMock(side_effect=fake_messages_create),
        ):
            response = await messages_endpoint(
                MessagesRequest(
                    model="FREE",
                    messages=[{"role": "user", "content": "hi"}],
                ),
                request,
            )

        self.assertIn("grok-4.3-console", response.body.decode("utf-8"))
        self.assertEqual(captured["model"], "grok-4.3-console")
        self.assertEqual(request.state.request_log_routing["resolved_model"], "grok-4.3-console")


if __name__ == "__main__":
    unittest.main()
