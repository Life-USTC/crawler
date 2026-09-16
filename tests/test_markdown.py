import unittest

from bs4 import BeautifulSoup

from ustc_crawler.markdown import (
    _strip_paragraph_layout_whitespace,
    html_to_markdown,
    image_source_hash,
    image_source_urls,
    local_image_url,
)


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

    def test_registered_images_use_local_urls_and_keep_repeated_positions(self) -> None:
        base_url = "https://www.ustc.edu.cn/info/1/2.htm"
        first = "https://www.ustc.edu.cn/a.jpg"
        second = "https://www.ustc.edu.cn/b.jpg"
        sources = {image_source_hash(url): url for url in (first, second)}
        html = (
            "<p><img data-src='/a.jpg' alt='第一张'>中间"
            "<img src='/b.jpg' alt='第二张'>末尾"
            "<img src='/a.jpg' alt='第一张重复'></p>"
        )

        md = html_to_markdown(
            html,
            base_url=base_url,
            image_sources=sources,
            strict_image_sources=True,
        )

        first_local = local_image_url(first)
        second_local = local_image_url(second)
        self.assertEqual(md.count(first_local), 2)
        self.assertEqual(md.count(second_local), 1)
        self.assertLess(md.index(first_local), md.index(second_local))
        self.assertLess(md.index(second_local), md.rindex(first_local))
        self.assertIn("![第一张]", md)
        self.assertIn("![第二张]", md)

    def test_strict_registered_images_drop_unknown_sources(self) -> None:
        known = "https://www.ustc.edu.cn/known.jpg"
        md = html_to_markdown(
            "<p><img src='/known.jpg' alt='已登记'><img src='/unknown.jpg' alt='未知'></p>",
            base_url="https://www.ustc.edu.cn/info/1/2.htm",
            image_sources={image_source_hash(known): known},
            strict_image_sources=True,
        )
        self.assertIn(local_image_url(known), md)
        self.assertNotIn("unknown.jpg", md)

    def test_nested_paragraph_layout_whitespace_does_not_double_indent(self) -> None:
        md = html_to_markdown("<p>\u3000<span>\xa0 </span><span>正文</span>之后</p>")
        self.assertEqual(md, "正文之后")

    def test_paragraph_cleanup_preserves_code_layout_whitespace(self) -> None:
        soup = BeautifulSoup("<p><code>  code\n  line</code></p>", "html.parser")
        _strip_paragraph_layout_whitespace(soup)
        self.assertEqual(soup.code.string, "  code\n  line")

    def test_relative_src_kept_without_base_url(self) -> None:
        html = "<div><p><img src='/__local/a.jpg' alt='图'></p></div>"
        md = html_to_markdown(html)
        self.assertIn("![图](/__local/a.jpg)", md)

    def test_relative_link_href_is_absolutized_with_base_url(self) -> None:
        html = (
            "<div><p><a href='/_upload/article/files/ab/cd/x.docx'>附件下载</a>"
            "<a href='../../0901/c1a2/page.htm'>相关文章</a></p></div>"
        )
        md = html_to_markdown(html, base_url="https://mba.ustc.edu.cn/2026/0729/c20772a748934/page.htm")
        self.assertIn(
            "[附件下载](https://mba.ustc.edu.cn/_upload/article/files/ab/cd/x.docx)", md
        )
        self.assertIn("[相关文章](https://mba.ustc.edu.cn/2026/0901/c1a2/page.htm)", md)

    def test_non_http_links_are_left_untouched(self) -> None:
        html = (
            "<div><p><a href='mailto:a@ustc.edu.cn'>邮箱</a>"
            "<a href='javascript:void(0)'>按钮</a>"
            "<a href='#section'>锚点</a></p></div>"
        )
        md = html_to_markdown(html, base_url="https://x.ustc.edu.cn/info/1/2.htm")
        self.assertIn("[邮箱](mailto:a@ustc.edu.cn)", md)
        self.assertIn("[按钮](javascript:void(0))", md)
        self.assertIn("[锚点](#section)", md)

    def test_relative_link_kept_without_base_url(self) -> None:
        html = "<div><p><a href='/_upload/x.docx'>附件</a></p></div>"
        md = html_to_markdown(html)
        self.assertIn("[附件](/_upload/x.docx)", md)

    def test_video_player_renders_link_to_media(self) -> None:
        html = (
            "<div class='wp_articlecontent'><p>"
            "<div class='wp_video_player' "
            "sudy-wp-src='/_upload/article/videos/f0/2f/1c19876f.mp4' "
            "sudyfile-attr=\"{'title':'2021032539014229.mp4'}\"></div>"
            "</p></div>"
        )
        md = html_to_markdown(html, base_url="https://biotraining.ustc.edu.cn/2022/0303/c26359a547647/page.htm")
        self.assertIn(
            "[视频: 2021032539014229.mp4]"
            "(https://biotraining.ustc.edu.cn/_upload/article/videos/f0/2f/1c19876f.mp4)",
            md,
        )

    def test_vurl_image_stays_an_image_and_video_poster_is_dropped(self) -> None:
        source_url = "https://news.ustc.edu.cn/images/vsb.jpg"
        html = (
            '<p><img vurl="https://news.ustc.edu.cn/media/video.mp4" '
            'src="/images/vsb.jpg" alt="现场"></p>'
            '<video poster="https://img-xhpfm.example/poster.jpg" '
            'src="/media/video.mp4"></video>'
        )
        md = html_to_markdown(
            html,
            base_url="https://news.ustc.edu.cn/info/1056/90586.htm",
            image_sources={image_source_hash(source_url): source_url},
            strict_image_sources=True,
        )

        self.assertIn(f"![现场]({local_image_url(source_url)})", md)
        self.assertIn("[视频](https://news.ustc.edu.cn/media/video.mp4)", md)
        self.assertNotIn("img-xhpfm.example", md)
        self.assertEqual(
            image_source_urls(html, base_url="https://news.ustc.edu.cn/info/1056/90586.htm"),
            (source_url,),
        )

    def test_pdf_player_renders_attachment_link(self) -> None:
        html = (
            "<div class='wp_articlecontent'><p>"
            "<span id='第十八届全国大学生数学竞赛报名的通知-科大版.pdf' class='wp_pdf_player' "
            "pdfsrc='/_upload/article/files/fe/65/9d1f6318.pdf' "
            "sudyfile-attr=\"{'title':'第十八届全国大学生数学竞赛报名的通知-科大版.pdf'}\"></span>"
            "</p></div>"
        )
        md = html_to_markdown(html, base_url="https://math.ustc.edu.cn/2026/0901/c18650a751735/page.htm")
        self.assertIn(
            "[附件: 第十八届全国大学生数学竞赛报名的通知-科大版.pdf]"
            "(https://math.ustc.edu.cn/_upload/article/files/fe/65/9d1f6318.pdf)",
            md,
        )

    def test_player_without_url_hint_uses_generic_label(self) -> None:
        html = (
            "<div><p><div class='wp_video_player' sudy-wp-src='/v/a.mp4'></div></p></div>"
        )
        md = html_to_markdown(html, base_url="https://x.ustc.edu.cn/info/1/2.htm")
        self.assertIn("[视频](https://x.ustc.edu.cn/v/a.mp4)", md)

    def test_vsb_pdf_image_data_script_becomes_images(self) -> None:
        html = (
            "<div class='v_news_content'><p style='text-indent: 0'>"
            "<script>var vsb_pdf_image_data = "
            "[\"/__local/0/13/5F/a.jpg\",\"/__local/4/98/0F/b.jpg\"];</script>"
            "</p></div>"
        )
        md = html_to_markdown(html, base_url="https://ef.ustc.edu.cn/info/1022/2374.htm")
        self.assertIn("![](https://ef.ustc.edu.cn/__local/0/13/5F/a.jpg)", md)
        self.assertIn("![](https://ef.ustc.edu.cn/__local/4/98/0F/b.jpg)", md)
        self.assertNotIn("vsb_pdf_image_data", md)

    def test_escaped_fckeditor_tags_are_restored_when_frequent(self) -> None:
        html = (
            "<div class='wp_articlecontent'><p>交流会活动。</p>"
            "&lt;IMG border=0 src=&quot;/_upload/article/images/a.jpg&quot; /&gt;"
            "&lt;IMG border=0 src=&quot;/_upload/article/images/b.jpg&quot; /&gt;"
            "&lt;IMG border=0 src=&quot;/_upload/article/images/c.jpg&quot; /&gt;"
            "</div>"
        )
        md = html_to_markdown(html, base_url="http://www.nsrl.ustc.edu.cn/2014/0917/c10984a121342/page.htm")
        # normalize_url upgrades ustc.edu.cn hosts to https (F3 dedup).
        self.assertIn("![](https://www.nsrl.ustc.edu.cn/_upload/article/images/a.jpg)", md)
        self.assertIn("![](https://www.nsrl.ustc.edu.cn/_upload/article/images/c.jpg)", md)
        self.assertNotIn("&lt;IMG", md)

    def test_unterminated_escaped_fckeditor_tags_are_restored(self) -> None:
        # The real 2014-era nsrl pages never wrote the closing ``&gt;``; the
        # escaped tag text runs straight into the enclosing paragraph end.
        html = (
            "<div class='wp_articlecontent'>"
            "<p style='text-align:center;'>&lt;IMG border=0 src=&quot;/_upload/a.jpg&quot; width=500</p>"
            "<p style='text-align:center;'>&lt;IMG border=0 src=&quot;/_upload/b.jpg&quot; width=500</p>"
            "<p style='text-align:center;'>&lt;IMG border=0 src=&quot;/_upload/c.jpg&quot; width=500</p>"
            "<p>参观结束。</p></div>"
        )
        md = html_to_markdown(html, base_url="http://www.nsrl.ustc.edu.cn/2014/0917/c10984a121342/page.htm")
        # normalize_url upgrades ustc.edu.cn hosts to https (F3 dedup).
        self.assertIn("![](https://www.nsrl.ustc.edu.cn/_upload/a.jpg)", md)
        self.assertIn("![](https://www.nsrl.ustc.edu.cn/_upload/c.jpg)", md)
        self.assertIn("参观结束。", md)
        self.assertNotIn("&lt;IMG", md)

    def test_isolated_escaped_tag_is_left_alone(self) -> None:
        html = (
            "<div><p>写法示例:&lt;img src=&quot;x.png&quot;&gt; 是图片标签。</p>"
            "<p>正文内容保持不变。</p></div>"
        )
        md = html_to_markdown(html, base_url="https://x.ustc.edu.cn/info/1/2.htm")
        self.assertIn('<img src="x.png">', md)
        self.assertNotIn("![", md)
        self.assertIn("正文内容保持不变。", md)

    def test_noise_elements_are_removed(self) -> None:
        html = (
            "<div class='wl-con wl-detail'>"
            "<div class='wl-post'><i></i>2026-04-26<i></i>访问次数：<span class='WP_VisitCount' "
            "url='/_visitcountdisplay?articleId=741222'>14</span>次</div>"
            "<div class='wp_articlecontent'><p>正文内容。</p></div>"
            "<p class='text-center'>2026-07-06 |  查看: <span class='WP_VisitCount'>15</span></p>"
            "<div class='social-share'><span class='share_tt'>分享至:</span></div>"
            "</div>"
        )
        md = html_to_markdown(html, base_url="https://nsti.ustc.edu.cn/2026/0520/c13715a741222/page.htm")
        self.assertIn("正文内容。", md)
        self.assertNotIn("访问次数", md)
        self.assertNotIn("查看", md)
        self.assertNotIn("分享至", md)

    def test_smile_metadata_bar_is_removed(self) -> None:
        html = (
            "<div class='central_text'>"
            "<span class='weix_time'><span>发布时间：2026-05-28</span><span>发布来源：</span></span>"
            "<div class='center_txt'><p>心理嘉年华活动正文。</p></div>"
            "</div>"
        )
        md = html_to_markdown(html, base_url="http://smile.ustc.edu.cn/index/info/5017")
        self.assertIn("心理嘉年华活动正文。", md)
        self.assertNotIn("发布时间", md)
        self.assertNotIn("发布来源", md)

    def test_joomla_pager_and_friend_links_are_removed(self) -> None:
        html = (
            "<div class='item-page'><p>通知正文。</p>"
            "<ul class='pager pagenav'><li class='next'>"
            "<a href='/index.php/newslists/news/116-x' rel='next'>下页 <span></span></a>"
            "</li></ul>"
            "<p class='linkstitle'>友情链接</p>"
            "<p class='bottomlinks'><a href='http://www.ahedu.gov.cn/'>安徽教育网</a></p>"
            "</div>"
        )
        md = html_to_markdown(html, base_url="https://utfd.ustc.edu.cn/index.php/newslists/news/117-2025-05-27-03-06-47")
        self.assertIn("通知正文。", md)
        self.assertNotIn("下页", md)
        self.assertNotIn("友情链接", md)
        self.assertNotIn("安徽教育网", md)

    def test_empty_headings_are_dropped(self) -> None:
        html = (
            "<div class='infobox'><h2 class='arti_title'></h2>"
            "<div class='wp_articlecontent'><p>正文内容。</p></div></div>"
        )
        md = html_to_markdown(html, base_url="https://lab.ustc.edu.cn/info/1/2.htm")
        self.assertEqual(md, "正文内容。")

    def test_heading_with_text_or_image_is_kept(self) -> None:
        html = (
            "<div><h2>真实小节标题</h2><p>内容一。</p>"
            "<h3><img src='/a.jpg' alt='海报'></h3><p>内容二。</p></div>"
        )
        md = html_to_markdown(html, base_url="https://x.ustc.edu.cn/info/1/2.htm")
        self.assertIn("## 真实小节标题", md)
        self.assertIn("![海报](https://x.ustc.edu.cn/a.jpg)", md)


if __name__ == "__main__":
    unittest.main()
