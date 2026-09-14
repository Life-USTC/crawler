"""Tests for the WordPress family adapter (library and friends)."""

from __future__ import annotations

import unittest


class WordPressAdapterTests(unittest.TestCase):
    def test_library_fixture(self) -> None:
        from pathlib import Path

        from ustc_crawler.adapters.wordpress import WordPressAdapter

        html = (Path(__file__).parent / "fixtures/adapters/wordpress/lib.html").read_text(
            encoding="utf-8"
        )
        fields = WordPressAdapter().extract("https://lib.ustc.edu.cn/?p=6092", html)
        self.assertIsNotNone(fields)
        self.assertEqual(fields.title, "【公告】致新生读者")
        self.assertEqual(fields.published_at, "2008-08-31")
        self.assertIn("一卡通", fields.body_html)

    def test_homepage_returns_none(self) -> None:
        from ustc_crawler.adapters.wordpress import WordPressAdapter

        self.assertIsNone(
            WordPressAdapter().extract(
                "https://lib.ustc.edu.cn/", "<html><body><p>首页</p></body></html>"
            )
        )

    def test_nested_date_element_inside_h2(self) -> None:
        from ustc_crawler.adapters.wordpress import WordPressAdapter

        html = (
            "<html><body><div class='detail-text'><h1>公告标题</h1>"
            "<h2><span>2026-01-05</span></h2>"
            "<p>这是图书馆公告正文内容，长度足够十个字以上。</p>"
            "</div></body></html>"
        )
        fields = WordPressAdapter().extract("https://lib.ustc.edu.cn/?p=1", html)
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.title, "公告标题")
        self.assertEqual(fields.published_at, "2026-01-05")

    def test_single_digit_month_is_zero_padded(self) -> None:
        from ustc_crawler.adapters.wordpress import WordPressAdapter

        html = (
            "<html><body><div class='detail-text'><h1>公告标题</h1>"
            "<h2>2026-1-5</h2>"
            "<p>这是图书馆公告正文内容，长度足够十个字以上。</p>"
            "</div></body></html>"
        )
        fields = WordPressAdapter().extract("https://lib.ustc.edu.cn/?p=1", html)
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.published_at, "2026-01-05")


if __name__ == "__main__":
    unittest.main()
