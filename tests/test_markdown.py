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

    def test_relative_image_src_is_absolutized_with_base_url(self) -> None:
        html = "<div><p><img src='/__local/a.jpg' alt='图'></p></div>"
        md = html_to_markdown(html, base_url="https://www.ustc.edu.cn/info/1/2.htm")
        self.assertIn("![图](https://www.ustc.edu.cn/__local/a.jpg)", md)

    def test_lazy_data_src_is_absolutized_with_base_url(self) -> None:
        html = "<div><p><img data-src='/__local/b.jpg' alt='懒加载'></p></div>"
        md = html_to_markdown(html, base_url="https://www.ustc.edu.cn/info/1/2.htm")
        self.assertIn("![懒加载](https://www.ustc.edu.cn/__local/b.jpg)", md)

    def test_srcset_candidates_are_absolutized_with_base_url(self) -> None:
        html = "<div><p><img src='/a.jpg' srcset='/a.jpg 1x, /a@2x.jpg 2x' alt='图'></p></div>"
        md = html_to_markdown(html, base_url="https://www.ustc.edu.cn/info/1/2.htm")
        self.assertIn("https://www.ustc.edu.cn/a.jpg", md)
        self.assertNotIn("srcset='/", md)

    def test_relative_src_kept_without_base_url(self) -> None:
        html = "<div><p><img src='/__local/a.jpg' alt='图'></p></div>"
        md = html_to_markdown(html)
        self.assertIn("![图](/__local/a.jpg)", md)


if __name__ == "__main__":
    unittest.main()
