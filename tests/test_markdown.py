import unittest

from ustc_crawler.markdown import html_to_markdown


class HtmlToMarkdownTests(unittest.TestCase):
    def test_headings_lists_tables_and_images_survive(self) -> None:
        html = (
            "<div><h2>一、总则</h2><p>第一段</p><ul><li>甲</li><li>乙</li></ul>"
            "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
            "<p><img src='https://x.ustc.edu.cn/a.png' alt='示意图'></p></div>"
        )
        md = html_to_markdown(html)
        self.assertIn("## 一、总则", md)
        self.assertIn("- 甲", md)
        self.assertIn("| A |", md)
        self.assertIn("![示意图](https://x.ustc.edu.cn/a.png)", md)

    def test_strip_selectors_remove_site_chrome(self) -> None:
        html = "<div><p>正文</p><div class='footer-sign'> XX大学 版权所有</div></div>"
        md = html_to_markdown(html, strip_selectors=(".footer-sign",))
        self.assertIn("正文", md)
        self.assertNotIn("版权所有", md)

    def test_script_style_and_base64_images_removed(self) -> None:
        html = (
            "<div><script>var x=1;</script><style>p{}</style>"
            "<img src='data:image/png;base64,AAAA'>"
            "<p>内容</p></div>"
        )
        md = html_to_markdown(html)
        self.assertEqual(md, "内容")

    def test_empty_input(self) -> None:
        self.assertEqual(html_to_markdown(""), "")


if __name__ == "__main__":
    unittest.main()
