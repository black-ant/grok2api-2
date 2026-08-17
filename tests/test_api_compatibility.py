import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.control.model import registry
from app.products.openai.router import image_edits, list_models, router, videos_generations
from app.products.openai.schemas import VideoGenerationRequest


class _Request:
    def __init__(self, *, headers=None):
        self.headers = headers or {}
        self.app = SimpleNamespace(state=SimpleNamespace(repository=None))

    async def json(self):
        return self.payload


class ApiCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_list_only_advertises_models_available_in_pool(self):
        request = _Request()

        async def fake_available_pools(_request):
            return frozenset({"basic"})

        with patch(
            "app.products.openai.router._available_pools",
            new=fake_available_pools,
        ), patch("app.control.model.aliases.get_config", return_value={}):
            response = await list_models(request)

        model_ids = {item["id"] for item in json.loads(response.body)["data"]}
        self.assertIn("grok-4.20-fast", model_ids)
        self.assertIn("grok-chat-fast", model_ids)
        self.assertIn("grok-composer-2.5-fast", model_ids)
        self.assertIn("grok-4.3", model_ids)
        self.assertIn("grok-4.3-console", model_ids)
        self.assertIn("grok-build-0.1", model_ids)
        self.assertIn("grok-imagine-image-2.0", model_ids)
        self.assertIn("grok-imagine-video-1.5", model_ids)
        self.assertIn("grok-voice-latest", model_ids)
        self.assertIn("grok-stt", model_ids)
        self.assertNotIn("grok-4.20-auto", model_ids)

        catalog = {item["id"]: item for item in json.loads(response.body)["data"]}
        self.assertTrue(catalog["grok-imagine-image-2.0"].get("supported_in_api", True))
        self.assertTrue(catalog["grok-voice-latest"].get("supported_in_api", True))

        expected_console_media_models = {
            "grok-imagine-image-quality",
            "grok-imagine-image-2.0",
            "grok-imagine-image-quality-2.0",
            "grok-imagine-video-1.5",
            "grok-voice-latest",
            "grok-voice-think-fast-1.0",
            "grok-voice-think-fast-2.0",
            "grok-stt",
        }
        self.assertTrue(expected_console_media_models <= model_ids)
        self.assertTrue(
            all(catalog[model_id]["supported_in_api"] for model_id in expected_console_media_models)
        )
        self.assertFalse(catalog["grok-composer-2.5-fast"]["supported_in_api"])

    async def test_web_chat_catalog_models_follow_remote_tiers(self):
        request = _Request()

        async def fake_available_pools(_request):
            return frozenset({"basic", "super", "heavy"})

        with patch(
            "app.products.openai.router._available_pools",
            new=fake_available_pools,
        ), patch("app.control.model.aliases.get_config", return_value={}):
            response = await list_models(request)

        model_ids = {item["id"] for item in json.loads(response.body)["data"]}
        self.assertTrue(
            {"grok-chat-fast", "grok-chat-auto", "grok-chat-expert", "grok-chat-heavy"}
            <= model_ids
        )

    def test_audio_and_voice_routes_are_registered(self):
        registered = {
            (route.path, method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        websocket_paths = {
            route.path
            for route in router.routes
            if route.__class__.__name__ == "APIWebSocketRoute"
        }
        self.assertIn(("/v1/audio/speech", "POST"), registered)
        self.assertIn(("/v1/audio/tasks", "POST"), registered)
        self.assertIn(("/v1/audio/transcriptions", "POST"), registered)
        self.assertIn(("/v1/tts", "POST"), registered)
        self.assertIn(("/v1/stt", "POST"), registered)
        self.assertIn(("/v1/tts/voices", "GET"), registered)
        self.assertIn(("/v1/tts/voices/{voice_id}", "GET"), registered)
        self.assertIn("/v1/realtime", websocket_paths)
        self.assertIn("/v1/stt", websocket_paths)

    async def test_codex_model_catalog_has_etag_and_supports_not_modified(self):
        request = _Request(headers={})

        async def fake_available_pools(_request):
            return frozenset({"basic", "super"})

        with patch(
            "app.products.openai.router._available_pools",
            new=fake_available_pools,
        ), patch("app.control.model.aliases.get_config", return_value={}):
            response = await list_models(request, client_version="0.145.0")

        payload = json.loads(response.body)
        self.assertTrue(payload["models"])
        self.assertIn("slug", payload["models"][0])
        self.assertIn("supported_reasoning_levels", payload["models"][0])
        etag = response.headers["etag"]

        cached_request = _Request(headers={"if-none-match": etag})
        with patch(
            "app.products.openai.router._available_pools",
            new=fake_available_pools,
        ), patch("app.control.model.aliases.get_config", return_value={}):
            cached_response = await list_models(
                cached_request,
                client_version="0.145.0",
            )

        self.assertEqual(cached_response.status_code, 304)
        self.assertEqual(cached_response.headers["etag"], etag)

    async def test_json_image_edit_accepts_data_uri_reference(self):
        request = _Request(headers={"content-type": "application/json"})
        request.payload = {
            "model": "grok-imagine-image-edit",
            "prompt": "edit",
            "image": "data:image/png;base64,AAAA",
        }
        resolved = SimpleNamespace(
            model="grok-imagine-image-edit",
            spec=registry.get("grok-imagine-image-edit"),
        )
        with patch(
            "app.products.openai.router._resolve_model_for_request",
            new=AsyncMock(return_value=resolved),
        ), patch(
            "app.products.openai.images.edit",
            new=AsyncMock(return_value={"created": 1, "data": []}),
        ) as edit_mock:
            response = await image_edits(request)
        self.assertEqual(response.status_code, 200)
        messages = edit_mock.await_args.kwargs["messages"]
        self.assertEqual(
            messages[0]["content"][1]["image_url"]["url"],
            "data:image/png;base64,AAAA",
        )

    async def test_json_video_generation_maps_aspect_ratio_and_references(self):
        request = _Request(headers={"Idempotency-Key": "video-request-1"})
        resolved = SimpleNamespace(
            model="grok-imagine-video",
            spec=registry.get("grok-imagine-video"),
        )
        request_data = VideoGenerationRequest(
            model="grok-imagine-video",
            prompt="a tracking shot",
            duration=10,
            aspect_ratio="16:9",
            resolution="720p",
            reference_images=[
                {"url": "data:image/png;base64,AAAA"},
            ],
        )
        with patch(
            "app.products.openai.router._resolve_model_for_request",
            new=AsyncMock(return_value=resolved),
        ), patch(
            "app.products.openai.video.create_video",
            new=AsyncMock(return_value={"id": "video_1"}),
        ) as create_mock:
            response = await videos_generations(request_data, request)
        self.assertEqual(response.status_code, 200)
        kwargs = create_mock.await_args.kwargs
        self.assertEqual(kwargs["size"], "1280x720")
        self.assertEqual(kwargs["resolution_name"], "720p")
        self.assertEqual(kwargs["idempotency_key"], "video-request-1")
        self.assertEqual(
            kwargs["input_references"],
            [{"image_url": "data:image/png;base64,AAAA"}],
        )


if __name__ == "__main__":
    unittest.main()
