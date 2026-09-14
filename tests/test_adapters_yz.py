import unittest
from pathlib import Path

from ustc_crawler.adapters.yz import YzAdapter


class YzAdapterTests(unittest.TestCase):
    def test_yz_fixture(self) -> None:
        html = (Path(__file__).parent / "fixtures/adapters/yz/yz.html").read_text(encoding="utf-8")
        fields = YzAdapter().extract(
            "https://yz.ustc.edu.cn/article/2827/181", html
        )
        self.assertIsNotNone(fields)
        self.assertEqual(fields.title, "我校召开2026年研究生招生复试工作会议")
        self.assertEqual(fields.published_at, "2026-03-18")
        self.assertIn("研究生招生复试工作会议", fields.body_html)

    def test_listing_page_returns_none(self) -> None:
        self.assertIsNone(
            YzAdapter().extract(
                "https://yz.ustc.edu.cn/column/181",
                "<html><body><p>列表</p></body></html>",
            )
        )

    def test_single_digit_month_is_zero_padded(self) -> None:
        html = (
            "<html><body><p class='zkd-title'>招生通知标题</p>"
            "<div class='provenance'>发布时间：2026-3-5</div>"
            "<div class='txt-new'><p>正文内容</p></div>"
            "</body></html>"
        )
        fields = YzAdapter().extract("https://yz.ustc.edu.cn/article/2827/181", html)
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.published_at, "2026-03-05")


if __name__ == "__main__":
    unittest.main()
