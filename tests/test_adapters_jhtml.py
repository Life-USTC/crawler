import unittest
from pathlib import Path

from ustc_crawler.adapters.jhtml import JhtmlAdapter


class JhtmlAdapterTests(unittest.TestCase):
    def test_iat_fixture(self) -> None:
        html = (Path(__file__).parent / "fixtures/adapters/jhtml/iat.html").read_text(encoding="utf-8")
        fields = JhtmlAdapter().extract(
            "https://iat.ustc.edu.cn/iat/xwdt/20230314/6731.html", html
        )
        self.assertIsNotNone(fields)
        self.assertIn("创客嘉年华", fields.title)
        self.assertEqual(fields.published_at, "2023-03-14")
        self.assertIn("科大讯飞", fields.body_html)

    def test_listing_page_returns_none(self) -> None:
        self.assertIsNone(
            JhtmlAdapter().extract(
                "https://iat.ustc.edu.cn/iat/xwdt/",
                "<html><body><p>列表</p></body></html>",
            )
        )

    def test_halfwidth_colon_date(self) -> None:
        html = (
            "<html><body><p class='News-detail-title'>标题</p>"
            "<div class='news-detail-notes'>发布时间: 2023-03-14 浏览次数: 10</div>"
            "<div class='news-detail-news-con'><p>正文内容</p></div>"
            "</body></html>"
        )
        fields = JhtmlAdapter().extract(
            "https://iat.ustc.edu.cn/iat/xwdt/20230314/6731.html", html
        )
        self.assertIsNotNone(fields)
        self.assertEqual(fields.published_at, "2023-03-14")


if __name__ == "__main__":
    unittest.main()
