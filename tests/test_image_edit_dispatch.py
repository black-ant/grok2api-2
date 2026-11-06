import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.control.model import registry
from app.platform.errors import ValidationError
from app.products.openai.router import _image_operation, chat_completions_endpoint
from app.products.openai.schemas import ChatCompletionRequest
from app.products.web.webui.chat import _capability_name

IMAGE_MODEL = "grok-imagine-image"
PRO_MODEL = "grok-imagine-image-pro"
LITE_MODEL = "grok-imagine-image-lite"
EDIT_MODEL = "grok-imagine-image-edit"
CONSOLE_MODEL = "grok-imagine-image-quality"


def _text_and_image_prompt() -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "make it night"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]


def _text_only_prompt() -> list[dict]:
    return [{"role": "user", "content": "a lighthouse at dawn"}]


class _Request:
    def __init__(self):
        self.headers = {}
        self.state = SimpleNamespace()
        self.app = SimpleNamespace(state=SimpleNamespace(repository=None))


class CapabilityDeclarationTests(unittest.TestCase):
    def test_web_image_models_declare_generation_and_edit(self):
        for name in (IMAGE_MODEL, PRO_MODEL):
            spec = registry.get(name)
            self.assertTrue(spec.is_image(), name)
            self.assertTrue(spec.is_image_edit(), name)

    def test_lite_and_dedicated_edit_models_stay_single_purpose(self):
        lite = registry.get(LITE_MODEL)
        self.assertTrue(lite.is_image())
        self.assertFalse(lite.is_image_edit())

        edit = registry.get(EDIT_MODEL)
        self.assertTrue(edit.is_image_edit())
        self.assertFalse(edit.is_image())

    def test_registry_has_no_duplicate_model_names(self):
        names = [spec.model_name for spec in registry.MODELS]
        self.assertEqual(len(names), len(set(names)))


class ImageOperationSelectionTests(unittest.TestCase):
    def test_plain_prompt_generates_even_for_edit_capable_models(self):
        for name in (IMAGE_MODEL, PRO_MODEL, CONSOLE_MODEL):
            self.assertEqual(_image_operation(registry.get(name), 0), "image", name)

    def test_reference_images_switch_to_edit_pipeline(self):
        for name in (IMAGE_MODEL, PRO_MODEL, CONSOLE_MODEL, EDIT_MODEL):
            self.assertEqual(_image_operation(registry.get(name), 1), "image_edit", name)

    def test_edit_only_model_without_reference_still_uses_edit(self):
        self.assertEqual(_image_operation(registry.get(EDIT_MODEL), 0), "image_edit")

    def test_non_editable_image_model_rejects_references(self):
        with self.assertRaises(ValidationError) as ctx:
            _image_operation(registry.get(LITE_MODEL), 2)
        self.assertEqual(ctx.exception.param, "messages")
        self.assertIn(LITE_MODEL, ctx.exception.message)


class ChatEndpointDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def _dispatch(self, model: str, messages: list[dict]):
        resolved = SimpleNamespace(
            model=model,
            spec=registry.get(model),
            is_virtual=False,
            pool="basic",
        )
        generate = AsyncMock(return_value={"data": []})
        edit = AsyncMock(return_value={"data": []})
        request = _Request()
        req = ChatCompletionRequest(model=model, messages=messages, stream=False)

        with patch(
            "app.products.openai.router._resolve_model_for_request",
            new=AsyncMock(return_value=resolved),
        ), patch("app.products.openai.images.generate", new=generate), patch(
            "app.products.openai.images.edit", new=edit
        ):
            await chat_completions_endpoint(req, request)

        return generate, edit, request

    async def test_reference_image_on_generation_model_calls_edit(self):
        generate, edit, request = await self._dispatch(IMAGE_MODEL, _text_and_image_prompt())

        generate.assert_not_awaited()
        edit.assert_awaited_once()
        self.assertEqual(request.state.request_log_operation, "image_edit")
        self.assertEqual(request.state.request_log_usage["media_input_images"], 1)

    async def test_plain_prompt_on_edit_capable_model_calls_generate(self):
        generate, edit, request = await self._dispatch(CONSOLE_MODEL, _text_only_prompt())

        generate.assert_awaited_once()
        edit.assert_not_awaited()
        self.assertEqual(request.state.request_log_operation, "image")
        self.assertNotIn("media_input_images", request.state.request_log_usage)

    async def test_explicit_edit_model_keeps_edit_pipeline(self):
        generate, edit, _request = await self._dispatch(EDIT_MODEL, _text_only_prompt())

        edit.assert_awaited_once()
        generate.assert_not_awaited()

    async def test_lite_model_with_reference_is_rejected(self):
        with self.assertRaises(ValidationError):
            await self._dispatch(LITE_MODEL, _text_and_image_prompt())


class WebUiCapabilityLabelTests(unittest.TestCase):
    def test_dual_models_are_listed_as_image_models(self):
        for name in (IMAGE_MODEL, CONSOLE_MODEL):
            self.assertEqual(_capability_name(registry.get(name)), "image", name)

    def test_edit_only_model_is_listed_as_image_edit(self):
        self.assertEqual(_capability_name(registry.get(EDIT_MODEL)), "image_edit")


if __name__ == "__main__":
    unittest.main()
