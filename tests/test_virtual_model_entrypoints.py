import unittest
from pathlib import Path


class VirtualModelEntrypointTests(unittest.TestCase):
    def test_anthropic_router_uses_virtual_model_resolver(self):
        source = Path("app/products/anthropic/router.py").read_text(encoding="utf-8")

        self.assertIn("from app.control.model import aliases as model_aliases", source)
        self.assertIn("resolved = model_aliases.resolve(", source)
        self.assertIn("model        = resolved.model", source)
        self.assertIn('"virtual_model"', source)
        self.assertNotIn("model_registry.get(req.model)", source)

    def test_video_service_does_not_use_direct_registry_get(self):
        source = Path("app/products/openai/video.py").read_text(encoding="utf-8")

        self.assertIn("from app.control.model import aliases as model_aliases", source)
        self.assertIn("resolved = model_aliases.resolve(model)", source)
        self.assertIn("real_model = resolved.model", source)
        self.assertNotIn("model_registry.get(model)", source)

    def test_debug_chat_models_include_virtual_models_first(self):
        source = Path("app/products/web/admin/__init__.py").read_text(encoding="utf-8")

        self.assertIn("virtual_models = [", source)
        self.assertIn("for resolved in model_aliases.list_virtual_models()", source)
        self.assertIn('"virtual": True', source)
        self.assertIn('"data": [*virtual_models, *models]', source)


if __name__ == "__main__":
    unittest.main()
