import unittest
from pathlib import Path

from ustc_crawler.adapters.yz import YzAdapter


class YzAdapterTests(unittest.TestCase):
    def test_yz_fixture(self) -> None:
        html = Path("tests/fixtures/adapters/yz/yz.html").read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
