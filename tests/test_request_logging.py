import unittest

from app.platform.request_logging import _env_prefixes, _path_matches


class RequestLoggingTests(unittest.TestCase):
    def test_debug_chat_path_is_logged_by_default(self):
        prefixes = _env_prefixes()

        self.assertTrue(_path_matches("/admin/api/debug/chat", prefixes))
        self.assertTrue(_path_matches("/admin/api/debug/chat/", prefixes))

    def test_other_admin_api_paths_are_not_logged_by_default(self):
        prefixes = _env_prefixes()

        self.assertFalse(_path_matches("/admin/api/request-logs", prefixes))
        self.assertFalse(_path_matches("/admin/api/config", prefixes))


if __name__ == "__main__":
    unittest.main()
