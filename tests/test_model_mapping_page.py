import unittest
from pathlib import Path


class ModelMappingPageTests(unittest.TestCase):
    def test_model_mapping_page_has_single_form_flow(self):
        html = Path("app/statics/admin/model-mapping.html").read_text(encoding="utf-8")

        self.assertIn("const VIRTUAL_MODELS = ['FREE', 'SUPER']", html)
        self.assertIn("apiFetch('/model-mapping'", html)
        self.assertIn("body:JSON.stringify({ models:{ aliases } })", html)
        self.assertNotIn("<table", html.lower())

    def test_admin_header_links_model_mapping_page(self):
        header = Path("app/statics/admin/header.html").read_text(encoding="utf-8")
        script = Path("app/statics/js/admin-header.js").read_text(encoding="utf-8")

        self.assertIn('/admin/model-mapping', header)
        self.assertIn('/admin/model-mapping', script)
        self.assertIn('model-mapping-nav-1', script)


if __name__ == "__main__":
    unittest.main()
