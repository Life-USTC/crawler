"""Tests for the vsb CMS family adapter (mech.ustc.edu.cn, iid.ustc.edu.cn)."""

from __future__ import annotations

import unittest
from pathlib import Path


class VsbAdapterTests(unittest.TestCase):
    def test_mech_fixture(self) -> None:
        from ustc_crawler.adapters.vsb import VsbCmsAdapter

        html = Path("tests/fixtures/adapters/vsb/mech.html").read_text(encoding="utf-8")
        adapter = VsbCmsAdapter()
        fields = adapter.extract(
            "https://mech.ustc.edu.cn/2022/0331/c4596a550763/page.htm", html
        )
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.title, "近代力学系教师例会（春季学期3月份）")
        self.assertEqual(fields.published_at, "2022-03-31")
        self.assertIn("教师例会", fields.body_html)

    def test_non_article_page_returns_none(self) -> None:
        from ustc_crawler.adapters.vsb import VsbCmsAdapter

        adapter = VsbCmsAdapter()
        self.assertIsNone(
            adapter.extract(
                "https://mech.ustc.edu.cn/",
                "<html><body><p>首页</p></body></html>",
            )
        )


if __name__ == "__main__":
    unittest.main()
