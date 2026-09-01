import unittest

from ustc_crawler.canonicalize import (
    host_matches,
    looks_like_asset,
    looks_like_binary,
    normalize_url,
)


class CanonicalizeTests(unittest.TestCase):
    def test_normalizes_tracking_and_fragment(self) -> None:
        value = normalize_url(
            "../news/2.htm?utm_source=x&b=2&a=1#part", "https://NEWS.USTC.EDU.CN/list/1.htm"
        )
        self.assertEqual(value, "https://news.ustc.edu.cn/news/2.htm?a=1&b=2")

    def test_host_matching_allows_subdomain(self) -> None:
        self.assertTrue(host_matches("https://dept.ustc.edu.cn/a", ["ustc.edu.cn"]))
        self.assertFalse(host_matches("https://example.com/a", ["ustc.edu.cn"]))

    def test_assets(self) -> None:
        self.assertTrue(looks_like_asset("https://a.ustc.edu.cn/a.pdf"))
        self.assertFalse(looks_like_asset("https://a.ustc.edu.cn/article/1"))

    def test_binary_signatures(self) -> None:
        self.assertTrue(looks_like_binary(b"PK\x03\x04office document"))
        self.assertTrue(looks_like_binary(b"%PDF-1.7"))
        self.assertFalse(looks_like_binary(b"<!doctype html><html>"))

    def test_rejects_malformed_port(self) -> None:
        self.assertEqual(
            normalize_url("https://example.ustc.edu.cn:bad/path"),
            "",
        )

    def test_strips_trailing_dns_dot(self) -> None:
        self.assertEqual(
            normalize_url("https://see.ustc.edu.cn./news/1.htm"),
            "https://see.ustc.edu.cn/news/1.htm",
        )
