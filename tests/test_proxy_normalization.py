import unittest
from urllib.parse import unquote, urlparse

from app.control.proxy.config import normalize_legacy_http_proxy


class LegacyProxyNormalizationTests(unittest.TestCase):
    def test_rewrites_plain_host_port_user_password(self):
        self.assertEqual(
            normalize_legacy_http_proxy("127.0.0.1:8080:user:pass"),
            "http://user:pass@127.0.0.1:8080",
        )

    def test_keeps_colons_inside_password(self):
        value = normalize_legacy_http_proxy("10.0.0.7:3128:user:pass:with:colons")
        parsed = urlparse(value)
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.hostname, "10.0.0.7")
        self.assertEqual(parsed.port, 3128)
        self.assertEqual(unquote(parsed.username), "user")
        self.assertEqual(unquote(parsed.password), "pass:with:colons")

    def test_escapes_credentials_needing_encoding(self):
        value = normalize_legacy_http_proxy("1.2.3.4:8080:user@domain:p@ss/?#%中文")
        parsed = urlparse(value)
        self.assertEqual(unquote(parsed.username), "user@domain")
        self.assertEqual(unquote(parsed.password), "p@ss/?#%中文")
        self.assertNotIn("中", value)

    def test_standard_urls_pass_through_unchanged(self):
        for url in (
            "http://user:pass@127.0.0.1:8080",
            "socks5h://user:token@resin:2260",
            "trojan://pass@proxy.example:443?security=tls#node",
        ):
            self.assertEqual(normalize_legacy_http_proxy(url), url)

    def test_non_ipv4_hosts_are_not_rewritten(self):
        for value in (
            "proxy.example:8080:user:pass",
            "[2001:db8::1]:8080:user:pass",
            "2001:db8::1:8080:user:pass",
        ):
            self.assertEqual(normalize_legacy_http_proxy(value), value)

    def test_incomplete_credentials_are_not_rewritten(self):
        for value in (
            "127.0.0.1:8080:user",
            "127.0.0.1:8080:user:",
            "127.0.0.1:8080::pass",
            "127.0.0.1",
            "",
        ):
            self.assertEqual(normalize_legacy_http_proxy(value), value)

    def test_invalid_ports_are_not_rewritten(self):
        for value in (
            "127.0.0.1:0:user:pass",
            "127.0.0.1:65536:user:pass",
            "127.0.0.1:http:user:pass",
            "127.0.0.1:-1:user:pass",
        ):
            self.assertEqual(normalize_legacy_http_proxy(value), value)

    def test_normalization_is_idempotent(self):
        once = normalize_legacy_http_proxy("127.0.0.1:8080:user:pass")
        self.assertEqual(normalize_legacy_http_proxy(once), once)

    def test_non_string_values_are_coerced(self):
        self.assertEqual(normalize_legacy_http_proxy(None), "")
        self.assertEqual(normalize_legacy_http_proxy(12345), "12345")


if __name__ == "__main__":
    unittest.main()
