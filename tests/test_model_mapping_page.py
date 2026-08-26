import unittest
from pathlib import Path


class ModelMappingPageTests(unittest.TestCase):
    def test_model_mapping_page_has_single_form_flow(self):
        html = Path("app/statics/admin/model-mapping.html").read_text(encoding="utf-8")

        self.assertIn("const VIRTUAL_MODELS = ['FREE', 'SUPER']", html)
        self.assertIn("const POOLS = ['stable', 'degraded']", html)
        self.assertIn("稳定池", html)
        self.assertIn("降级池", html)
        self.assertIn("apiFetch('/model-mapping'", html)
        self.assertIn("<span>降级</span>", html)
        self.assertIn("degradationStatus(item)", html)
        self.assertIn("loadModelsModal(false)", html)
        self.assertIn("openFreeCheckModal()", html)
        self.assertIn('id="free-check-modal"', html)
        self.assertIn('id="free-check-list"', html)
        self.assertIn("data-free-check-model", html)
        self.assertIn("FREE 模型检测", html)
        self.assertIn("apiFetch('/grok-capability/scan'", html)
        self.assertIn("selection_mode: 'routing'", html)
        self.assertIn("body:JSON.stringify({ models:{ aliases } })", html)
        self.assertIn("id=\"restore-btn\"", html)
        self.assertIn("const DEFAULT_ALIASES =", html)
        self.assertNotIn("id=\"clear-btn\"", html)
        self.assertNotIn("<table", html.lower())

    def test_model_routing_page_is_read_only_and_auto_refreshing(self):
        html = Path("app/statics/admin/model-routing.html").read_text(encoding="utf-8")
        router = Path("app/products/web/router.py").read_text(encoding="utf-8")
        admin_api = Path("app/products/web/admin/__init__.py").read_text(encoding="utf-8")

        self.assertIn('data-active="/admin/model-routing"', html)
        self.assertIn("apiFetch('/model-routing')", html)
        self.assertIn("window.setInterval(loadRouting,2000)", html)
        self.assertIn("最近实际路由", html)
        self.assertIn("升降级记录", html)
        self.assertIn("最近 50 条", html)
        self.assertIn("formatPoolEvents(alias.pool_events||[])", html)
        self.assertIn("统计范围：当前进程", html)
        self.assertIn('class="model-copy-btn"', html)
        self.assertIn('data-copy-model=', html)
        self.assertIn("navigator.clipboard?.writeText", html)
        self.assertIn("showToast('已复制','success')", html)
        self.assertNotIn("<form", html.lower())
        self.assertNotIn("<table", html.lower())
        self.assertIn('@router.get("/admin/model-routing"', router)
        self.assertIn('@router.get("/model-routing"', admin_api)
        self.assertIn("model_status_snapshot()", admin_api)
    def test_admin_header_links_model_mapping_page(self):
        header = Path("app/statics/admin/header.html").read_text(encoding="utf-8")
        script = Path("app/statics/js/admin-header.js").read_text(encoding="utf-8")

        self.assertIn('/admin/model-mapping', header)
        self.assertIn('/admin/model-mapping', script)
        self.assertIn('/admin/model-routing', header)
        self.assertIn('/admin/model-routing', script)
        self.assertIn('model-routing-nav-1', script)


if __name__ == "__main__":
    unittest.main()
