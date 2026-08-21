import unittest
from pathlib import Path


class AdminClashPageTests(unittest.TestCase):
    def setUp(self):
        self.html = Path("app/statics/admin/clash.html").read_text(encoding="utf-8")

    def test_pool_table_is_primary_and_import_is_in_modal(self):
        pool_index = self.html.index("id='clash-pool-panel'")
        modal_index = self.html.index("id='clash-import-modal'")

        self.assertLess(pool_index, modal_index)
        self.assertIn("id='clash-pool-rows'", self.html)
        self.assertIn("id='clash-open-import'", self.html)
        self.assertIn("id='clash-pool-test'", self.html)
        self.assertIn("id='clash-yaml'", self.html)
        self.assertIn("class='modal-overlay'", self.html)

    def test_draft_and_committed_payloads_use_separate_render_paths(self):
        self.assertIn("applyDraftPayload(payload)", self.html)
        self.assertIn("applyPoolPayload(payload)", self.html)
        self.assertIn("closeImportModal();", self.html)
        self.assertIn("renderPoolRows();", self.html)
        self.assertIn("testPoolProxies()", self.html)
        self.assertIn("formal_pool: true", self.html)


if __name__ == "__main__":
    unittest.main()
