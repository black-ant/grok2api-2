import unittest
from tempfile import TemporaryDirectory

import orjson

from app.platform.request_logging import RequestLogStore, _env_prefixes, _path_matches


class RequestLoggingTests(unittest.TestCase):
    def test_request_logs_page_shows_routing_columns(self):
        from pathlib import Path

        html = Path("app/statics/admin/request-logs.html").read_text(encoding="utf-8")

        self.assertIn("<th>Key后5</th>", html)
        self.assertIn("<th>账号</th>", html)
        self.assertIn("item.routing?.routed_key_tail", html)
        self.assertIn("item.routing?.pool", html)

    def test_debug_chat_path_is_logged_by_default(self):
        prefixes = _env_prefixes()

        self.assertTrue(_path_matches("/admin/api/debug/chat", prefixes))
        self.assertTrue(_path_matches("/admin/api/debug/chat/", prefixes))

    def test_other_admin_api_paths_are_not_logged_by_default(self):
        prefixes = _env_prefixes()

        self.assertFalse(_path_matches("/admin/api/request-logs", prefixes))
        self.assertFalse(_path_matches("/admin/api/config", prefixes))

    def test_request_log_store_paginates_by_offset(self):
        with TemporaryDirectory() as tmp:
            from pathlib import Path

            store = RequestLogStore(directory=Path(tmp))
            log_date = store.retained_dates()[0]
            path = store._path_for_date(log_date)
            path.parent.mkdir(parents=True, exist_ok=True)
            entries = [
                {"id": str(index), "log_date": log_date, "created_ts": float(index)}
                for index in range(5)
            ]
            path.write_bytes(b"".join(orjson.dumps(entry) + b"\n" for entry in entries))

            page = store._read_items_locked(limit=2, offset=2)

        self.assertEqual([item["id"] for item in page], ["2", "1"])

    def test_request_log_store_preserves_routing_metadata(self):
        with TemporaryDirectory() as tmp:
            from pathlib import Path

            store = RequestLogStore(directory=Path(tmp))
            entry = {
                "id": "route-test",
                "log_date": store.retained_dates()[0],
                "created_ts": 1.0,
                "routing": {
                    "model": "grok-3",
                    "routed_key": "abcd1234...wxyz9876",
                    "routed_key_tail": "z9876",
                    "pool": "super",
                    "mode_id": 1,
                },
            }

            store._write_entry_locked(entry)
            page = store._read_items_locked(limit=1)

        self.assertEqual(page[0]["routing"]["model"], "grok-3")
        self.assertEqual(page[0]["routing"]["routed_key"], "abcd1234...wxyz9876")
        self.assertEqual(page[0]["routing"]["routed_key_tail"], "z9876")
        self.assertEqual(page[0]["routing"]["pool"], "super")


if __name__ == "__main__":
    unittest.main()
