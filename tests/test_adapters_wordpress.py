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


if __name__ == "__main__":
    unittest.main()
