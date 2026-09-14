"""Tests for the vsb CMS family adapter (mech.ustc.edu.cn, iid.ustc.edu.cn)."""

from __future__ import annotations

import unittest
from pathlib import Path


class VsbAdapterTests(unittest.TestCase):
    def test_mech_fixture(self) -> None:
        from ustc_crawler.adapters.vsb import VsbCmsAdapter

        html = (Path(__file__).parent / "fixtures/adapters/vsb/mech.html").read_text(encoding="utf-8")
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

    def test_arti_metas_date_wins_over_earlier_page_text(self) -> None:
        from ustc_crawler.adapters.vsb import VsbCmsAdapter

        html = (
            "<html><body>"
            "<div class='sidebar'>相关文章 发布时间：2020-01-01</div>"
            "<p class='arti_title'>力学系例会</p>"
            "<p class='arti_metas'><span class='arti_update'>发布时间：2022-03-31</span></p>"
            "<div class='wp_articlecontent'><p>正文内容</p></div>"
            "</body></html>"
        )
        fields = VsbCmsAdapter().extract(
            "https://mech.ustc.edu.cn/2022/0331/c4596a550763/page.htm", html
        )
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.published_at, "2022-03-31")

    def test_full_page_date_search_remains_fallback(self) -> None:
        from ustc_crawler.adapters.vsb import VsbCmsAdapter

        html = (
            "<html><body>"
            "<p class='arti_title'>力学系例会</p>"
            "<div class='wp_articlecontent'><p>正文内容 发布时间：2022-3-1</p></div>"
            "</body></html>"
        )
        fields = VsbCmsAdapter().extract(
            "https://mech.ustc.edu.cn/2022/0301/c4596a550763/page.htm", html
        )
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.published_at, "2022-03-01")


if __name__ == "__main__":
    unittest.main()
