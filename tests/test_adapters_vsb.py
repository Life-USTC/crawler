"""Tests for the vsb CMS family adapter (mech/iid/nsrl/physics.ustc.edu.cn)."""

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

    def test_nsrl_host_is_registered(self) -> None:
        from ustc_crawler.adapters import adapter_for

        adapter = adapter_for(
            "unit-www-nsrl-ustc-edu-cn",
            "https://www.nsrl.ustc.edu.cn/2026/0831/c10982a751691/page.htm",
        )
        self.assertIsNotNone(adapter)
        assert adapter is not None
        self.assertEqual(adapter.name, "vsb")

    def test_nsrl_fixture_excludes_sidebar_portlet(self) -> None:
        from ustc_crawler.adapters.vsb import VsbCmsAdapter

        html = (Path(__file__).parent / "fixtures/adapters/vsb/nsrl.html").read_text(encoding="utf-8")
        fields = VsbCmsAdapter().extract(
            "https://www.nsrl.ustc.edu.cn/2026/0831/c10982a751691/page.htm", html
        )
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.title, "加速器-软件学院研究生交流会成功举办")
        self.assertEqual(fields.published_at, "2016-11-19")
        self.assertIn("软件学院共50余名同学", fields.body_html)
        self.assertNotIn("最新推荐", fields.body_html)

    def test_physics_fixture_strips_bracketed_date_suffix(self) -> None:
        from ustc_crawler.adapters import adapter_for

        html = (Path(__file__).parent / "fixtures/adapters/vsb/physics.html").read_text(encoding="utf-8")
        url = "http://physics.ustc.edu.cn/2025/0919/c3586a701750/page.htm"
        adapter = adapter_for("unit-physics-ustc-edu-cn", url)
        self.assertIsNotNone(adapter)
        assert adapter is not None
        fields = adapter.extract(url, html)
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.title, "我院郭光灿院士团队柳必恒研究组利用高维纠缠实现高效量子随机通信")
        self.assertIn("高维纠缠", fields.body_html)


if __name__ == "__main__":
    unittest.main()
