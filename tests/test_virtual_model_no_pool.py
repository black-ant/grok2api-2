import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.products.openai.router import ChatCompletionRequest, chat_completions_endpoint


class _RequestState(SimpleNamespace):
    pass


class _Request(SimpleNamespace):
    pass


class _DummyConfig:
    def get_bool(self, _key, default=False):
        return default


class VirtualModelNoPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_resolves_before_downstream_account_selection_when_pool_is_empty(self):
        request = _Request(
            app=SimpleNamespace(state=SimpleNamespace(repository=None)),
            state=_RequestState(),
        )
        captured = {}

        async def fake_available_pools(_request):
            return frozenset()

        async def fake_chat_completions(**kwargs):
            captured.update(kwargs)
            return {'ok': True, 'model': kwargs['model']}

        with (
            patch('app.control.model.aliases.get_config', return_value={
                'FREE': ['grok-4.3-console'],
            }),
            patch('app.platform.config.snapshot.get_config', return_value=_DummyConfig()),
            patch(
                'app.products.openai.router._available_pools',
                new=fake_available_pools,
            ),
            patch(
                'app.products.openai.router.chat_completions',
                new=AsyncMock(side_effect=fake_chat_completions),
            ),
        ):
            response = await chat_completions_endpoint(
                ChatCompletionRequest(
                    model='FREE',
                    messages=[{'role': 'user', 'content': 'hi'}],
                ),
                request,
            )

        self.assertIn('grok-4.3-console', response.body.decode('utf-8'))
        self.assertEqual(captured['model'], 'grok-4.3-console')
        self.assertEqual(
            request.state.request_log_routing['resolved_model'],
            'grok-4.3-console',
        )


if __name__ == '__main__':
    unittest.main()
