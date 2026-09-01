import unittest

from ustc_crawler.canonicalize import (
    host_matches,
    looks_like_asset,
    looks_like_binary,
    looks_like_uploaded_html_attachment,
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

    def test_ustc_uploaded_html_attachment_is_an_asset(self) -> None:
        attachment = (
            "http://scc.ustc.edu.cn/_upload/article/files/7d/f9/"
            "033cd3b84a9d8a16b2b2eb9987e6/W020150417520333865223.htm"
        )
        article = "http://scc.ustc.edu.cn/2009/1014/c396a3060/page.htm"
        self.assertTrue(looks_like_uploaded_html_attachment(attachment))
        self.assertTrue(looks_like_asset(attachment))
        self.assertFalse(looks_like_uploaded_html_attachment(article))
        self.assertFalse(looks_like_asset(article))

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
