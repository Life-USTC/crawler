import unittest
from pathlib import Path

from ustc_crawler.crawl import _decode, _xml_links
from ustc_crawler.extract import extract_page, parse_date

SUZHOU_FIXTURE = Path(__file__).parent / "fixtures" / "suzhou_article.html"
SUZHOU_ENGLISH_FIXTURE = Path(__file__).parent / "fixtures" / "suzhou_english_article.html"


class ExtractTests(unittest.TestCase):
    def test_decode_prefers_real_chinese_encoding_over_bad_header(self) -> None:
        body = "<html><meta charset='gbk'><p>中国科大</p></html>".encode("gbk")
        self.assertIn("中国科大", _decode(body, {"content-type": "text/html; charset=iso-8859-1"}))

    def test_sitemap_links(self) -> None:
        body = b"<urlset><url><loc>/info/1/2.htm</loc></url><url><loc>https://news.ustc.edu.cn/a</loc></url></urlset>"
        self.assertEqual(
            _xml_links(body, {"content-type": "application/xml"}, "https://news.ustc.edu.cn/"),
            ["https://news.ustc.edu.cn/info/1/2.htm", "https://news.ustc.edu.cn/a"],
        )

    def test_sitemap_links_accept_legacy_multibyte_xml(self) -> None:
        body = "<?xml version='1.0' encoding='gb2312'?><urlset><url><loc>/通知/1.htm</loc></url></urlset>".encode("gb2312")
        self.assertEqual(
            _xml_links(
                body,
                {"content-type": "application/xml; charset=gb2312"},
                "https://example.ustc.edu.cn/",
            ),
            ["https://example.ustc.edu.cn/通知/1.htm"],
        )

    def test_list_link_date_hint(self) -> None:
        html = "<html><body><ul><li><a href='/article/3509'>新闻标题</a><span>2026-07-21</span></li></ul></body></html>"
        page = extract_page("https://gradschool.ustc.edu.cn/column/10", html)
        self.assertEqual(
            page.link_dates["https://gradschool.ustc.edu.cn/article/3509"], "2026-07-21"
        )

    def test_list_link_effective_date_is_not_publication_hint(self) -> None:
        html = """
        <html><body><ul><li><span><a href='/info/1029/25470.htm'>
        校园班车运行时刻表（2026年8月30日试运行）</a></span></li></ul></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/ggfw/rdlj.htm", html)
        self.assertNotIn("https://www.ustc.edu.cn/info/1029/25470.htm", page.link_dates)

    def test_recovers_nested_legacy_href(self) -> None:
        html = """
        <html><body><a href="<a href='/2026/0319/c30301a723546/page.htm'
        target='_blank' title='公告'>公告</a>"><span>2026-03-19</span></a></body></html>
        """
        page = extract_page("https://marx.ustc.edu.cn/main.htm", html)
        self.assertIn(
            "https://marx.ustc.edu.cn/2026/0319/c30301a723546/page.htm", page.links
        )

    def test_ignores_javascript_placeholder_links(self) -> None:
        page = extract_page(
            "https://example.ustc.edu.cn/main.htm",
            "<html><body><a href=\"${v_link('%27/\">错误占位符</a></body></html>",
        )
        self.assertEqual(page.links, [])

    def test_onclick_window_open_targets_are_links(self) -> None:
        # yz.ustc.edu.cn column pages navigate via
        # onclick="window.open('/article/2847/181?num=-1','_blank')" on the
        # list items instead of <a href>; they are the page's only outlinks.
        html = """
        <html><body><ul class='article-list'>
        <li onclick="window.open('/article/2847/181?num=-1','_blank')"><img src='/a.jpg'>2026年硕士招生简章</li>
        <li onclick="window.open('/article/2827/181?num=-1','_blank')"><img src='/b.jpg'>2026年博士招生通告</li>
        <li onclick="history.back()">返回</li>
        </ul></body></html>
        """
        page = extract_page("https://yz.ustc.edu.cn/column/181", html)
        self.assertIn("https://yz.ustc.edu.cn/article/2847/181?num=-1", page.links)
        self.assertIn("https://yz.ustc.edu.cn/article/2827/181?num=-1", page.links)
        self.assertEqual(len(page.links), 2)

    def test_detail_h1_replaces_generic_section_heading(self) -> None:
        html = """
        <html><head><title>真实标题 : 单位网站</title>
        <meta property='og:title' content='新闻速递'></head>
        <body><main><h1>新闻速递</h1><article><h1>真实标题</h1>
        <p>这是正文内容，足够长以便页面被识别为公开文章。</p></article></main></body></html>
        """
        page = extract_page("https://example.ustc.edu.cn/info/1/2.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "真实标题")

    def test_legacy_title_and_content_ids_are_used(self) -> None:
        html = """
        <html><head><title>单位网站</title></head><body>
        <div id='left'><a href='view_news.aspx?id=1'>旧文章</a></div>
        <div id='right'><span id='Title'>真正的通知标题</span>
        <div id='Content'><p>这是来自旧版栏目页的正文内容，包含通知事项和报名说明，长度足以被识别为公开文章。</p>
        <p>第二段正文保留在本地检索索引中。</p></div></div>
        </body></html>
        """
        page = extract_page("https://journal.ustc.edu.cn/ch/reader/view_news.aspx?id=20260602112551001", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "真正的通知标题")
        self.assertIn("报名说明", page.article.body_text)

    def test_suzhou_hidden_content_and_article_title_selectors(self) -> None:
        page = extract_page(
            "https://sz.ustc.edu.cn/xwgg_show/2512.html",
            SUZHOU_FIXTURE.read_text(encoding="utf-8"),
            source_id="suzhou",
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "2010年诺贝尔物理学奖得主做客我校大师论坛")
        self.assertEqual(page.article.published_at, "2026-09-01")
        self.assertIn("应邀做客我校大师论坛", page.article.body_text)
        self.assertNotIn("网站导航", page.article.body_text)
        self.assertNotIn("网站版权信息", page.article.body_text)

    def test_suzhou_english_hidden_content_and_article_title_selector(self) -> None:
        page = extract_page(
            "https://sz.ustc.edu.cn/en/en_news_show/74.html",
            SUZHOU_ENGLISH_FIXTURE.read_text(encoding="utf-8"),
            source_id="suzhou",
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "University of Technology Sydney Delegation Visits SIAR")
        self.assertEqual(page.article.published_at, "2025-11-18")
        self.assertIn("delegation from the University of Technology Sydney visited SIAR", page.article.body_text)
        self.assertNotIn("Links", page.article.title)
        self.assertNotIn("Site navigation", page.article.body_text)
        self.assertNotIn("Home", page.article.body_text)

    def test_wordpress_page_title_overrides_section_heading(self) -> None:
        html = """
        <html><head><title>单位网站</title></head><body>
        <div class='page_header'><h1>新闻速递</h1></div>
        <article><h1 class='page_title'>我校与合作伙伴举行线上会谈</h1>
        <p>这是正文内容，包含公开会谈信息和后续合作安排，长度足以被识别为文章。</p></article>
        </body></html>
        """
        page = extract_page("https://oic.ustc.edu.cn/news/detail/19762", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "我校与合作伙伴举行线上会谈")

    def test_university_meta_title_drops_site_suffix(self) -> None:
        html = """
        <html><head><title>校园班车运行时刻表（2026年8月30日试运行）-中国科学技术大学</title>
        <meta property='og:title' content='校园班车运行时刻表（2026年8月30日试运行）-中国科学技术大学'></head>
        <body><div class='v_news_content'><p>这是足够长的班车时刻表正文内容，用于验证标题中的站点名称不会进入文章标题。</p></div></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1029/25470.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "校园班车运行时刻表（2026年8月30日试运行）")

    def test_document_title_drops_compact_department_site_suffix(self) -> None:
        html = """
        <html><head><title>党委宣传部党支部集体观看高校党组织示范微党课-党委宣传部 新闻中心</title></head>
        <body><div class='wp_articlecontent'><p>这是足够长的新闻正文，用于验证无空格连字符后的单位站名不会进入文章标题。</p></div></body></html>
        """
        page = extract_page("https://xcb.ustc.edu.cn/info/1003/27066.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "党委宣传部党支部集体观看高校党组织示范微党课")

    def test_article_heading_wins_over_document_theme_suffix(self) -> None:
        html = """
        <html><head><title>新闻正文标题-学习贯彻主题教育</title></head><body>
        <main><h3>中央精神</h3><h3>新闻正文标题</h3>
        <div class='wp_articlecontent'><p>这是足够长的正文内容，用来验证专题网站的栏目名称不会进入文章标题。</p></div></main>
        </body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/theme/info/1002/1726.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "新闻正文标题")

    def test_theme_suffix_does_not_replace_title_with_navigation_heading(self) -> None:
        html = """
        <html><head><title>党建评：要谦虚，不要凌空蹈虚-树立和践行正确政绩观学习教育</title></head><body>
        <main><h3>党建评</h3>
        <div class='wp_articlecontent'><p>这是足够长的新闻正文，用来确认页面内的导航标题不会覆盖完整的文档标题。</p></div></main>
        </body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/zqzjg/info/1002/1407.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "党建评：要谦虚，不要凌空蹈虚")

    def test_theme_suffix_preserves_separators_inside_article_title(self) -> None:
        html = """
        <html><head><title>中国科大实现基于无腔冷原子系综的长距离原子-光子纠缠分发-学习贯彻主题教育</title></head><body>
        <div class='wp_articlecontent'><p>这是足够长的新闻正文，用来确认真实标题内部的连字符不会被站点后缀规则截断。</p></div>
        </body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/theme/info/1005/2129.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(
            page.article.title,
            "中国科大实现基于无腔冷原子系综的长距离原子-光子纠缠分发",
        )

    def test_detail_heading_preserves_legitimate_hyphenated_title(self) -> None:
        html = """
        <html><head><title>站点标题</title></head><body>
        <h1 class='article-title'>中国科学技术大学-美国天普大学联合培养项目通知</h1>
        <div class='wp_articlecontent'><p>这是足够长的项目通知正文，连字符属于真实标题内容，不能被站名清理规则删除。</p></div>
        </body></html>
        """
        page = extract_page("https://teach.ustc.edu.cn/notice/3132.html", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "中国科学技术大学-美国天普大学联合培养项目通知")

    def test_document_title_preserves_legitimate_hyphenated_title(self) -> None:
        html = """
        <html><head><title>中国科学技术大学-美国天普大学联合培养项目通知</title></head><body>
        <div class='wp_articlecontent'><p>这是足够长的项目通知正文，文档标题中的连字符属于真实标题内容，不能被站名清理规则删除。</p></div>
        </body></html>
        """
        page = extract_page("https://teach.ustc.edu.cn/notice/3132.html", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "中国科学技术大学-美国天普大学联合培养项目通知")

    def test_document_title_preserves_legitimate_colon(self) -> None:
        html = """
        <html><head><title>基金委通知：湖北人形机器人联合基金重大专项-中国科学技术大学</title></head><body>
        <div class='wp_articlecontent'><p>这是足够长的科研通知正文，用于验证真实标题中的中文冒号及其后内容不会被截断。</p></div>
        </body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1362/25343.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "基金委通知：湖北人形机器人联合基金重大专项")

    def test_site_heading_does_not_override_concrete_document_title(self) -> None:
        html = """
        <html><head><title>中国科大男子排球队挺进省运会排球赛（高校部）决赛</title></head>
        <body><div class='page-header'><h1>体育教学中心</h1></div>
        <div class='wp_articlecontent'><p>在省运会排球赛中，中国科大男子排球队发挥出色并进入决赛，正文长度足以识别为公开文章。</p></div>
        </body></html>
        """
        page = extract_page("https://www.tj.ustc.edu.cn/2010/0816/c1459a6956/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "中国科大男子排球队挺进省运会排球赛（高校部）决赛")

    def test_central_text_content_container_strips_site_shell_and_repeated_title(self) -> None:
        html = """
        <html><head><title>中国科学技术大学心理健康教育与咨询中心</title></head>
        <body><nav>网站首页 中心概况 资讯动态</nav>
        <div class='central_text'>
          <span class='center_titlea'>心理中心举办心理委员户外素质拓展活动</span>
          <span>您现在的位置：首页 &gt; 微笑报道</span>
          <p>为完善我校心理健康教育工作体系，心理健康教育与咨询中心组织开展了心理委员户外素质拓展活动，正文内容足够长。</p>
        </div></body></html>
        """
        page = extract_page("http://smile.ustc.edu.cn/index/info/4914", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "心理中心举办心理委员户外素质拓展活动")
        self.assertNotIn("网站首页", page.article.body_text)
        self.assertNotIn("您现在的位置", page.article.body_text)
        self.assertNotIn(page.article.title, page.article.body_text)

    def test_detail_heading_drops_labeled_publication_date(self) -> None:
        html = """
        <html><head><title>中国科大迎新管理</title></head><body>
        <div class='newstitle'><h2>中国科大2026年本科招生培养亮点发布</h2>
        <p>发表日期：2026年06月15日</p></div>
        <div class='newscont'><p>这是足够长的招生新闻正文，用于验证标题容器中的发表日期不会粘到文章标题里。</p></div>
        </body></html>
        """
        page = extract_page("https://welcome.ustc.edu.cn/web/news/182", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "中国科大2026年本科招生培养亮点发布")

    def test_document_title_wins_over_rich_text_h2_paragraph(self) -> None:
        html = """
        <html><head><title>劳动淬炼成长，实践书写青春——2026年春季学期“美食与生活”劳动实践课结课</title></head>
        <body><div class='biaoti_top'><h3>劳动淬炼成长，实践书写青春——2026年春季学期“美食与生活”劳动实践课结课</h3></div>
        <div class='wp_articlecontent'><p>近日，劳动实践课完成春季学期全部教学任务，课程覆盖五个校区。</p>
        <h2><p>全域覆盖，五校区联动共育。本学期课程继续打破空间限制与校区隔阂，这一整段正文不能成为文章标题。</p></h2></div>
        </body></html>
        """
        page = extract_page(
            "https://zsb.ustc.edu.cn/2026/0710/c35498a747019/page.htm",
            html,
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(
            page.article.title,
            "劳动淬炼成长，实践书写青春——2026年春季学期“美食与生活”劳动实践课结课",
        )

    def test_concrete_document_title_wins_over_unscoped_body_heading(self) -> None:
        html = """
        <html><head><title>中国科大在等离子体湍流研究领域取得突破</title></head>
        <body><div class='wp_articlecontent'><h2><p>日前，研究团队在磁约束聚变等离子体湍流输运研究中取得突破性进展，这是一整段正文。</p></h2></div></body></html>
        """
        page = extract_page(
            "https://physics.ustc.edu.cn/2024/0223/c3586a630461/page.htm",
            html,
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "中国科大在等离子体湍流研究领域取得突破")

    def test_empty_first_h1_does_not_hide_short_detail_heading(self) -> None:
        html = """
        <html><head><title>校名-中国科学技术大学党建与思政网</title></head><body>
        <h1></h1><section><h1>校名</h1>
        <div class='wp_articlecontent'><p>这是足够长的学校标识介绍正文，用于验证空标题节点不会隐藏后面的真实短标题。</p></div></section>
        </body></html>
        """
        page = extract_page("https://djyszw.ustc.edu.cn/info/1070/6836.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "校名")

    def test_image_only_article_container_does_not_fall_back_to_page_shell(self) -> None:
        html = """
        <html><head><title>校园班车运行时刻表-中国科学技术大学</title></head>
        <body><nav>首页 | 科大新闻 | 学校概况 | 院系介绍</nav>
        <div class='v_news_content'><p><img src='/__local/timetable.jpg'></p></div>
        <footer>Copyright 中国科学技术大学 皖ICP备05002528号
        <img src='/template/line.jpg'></footer></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1029/25470.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.body_text, "")
        self.assertEqual(
            [image.url for image in page.article.images],
            ["https://www.ustc.edu.cn/__local/timetable.jpg"],
        )

    def test_legacy_content_in_row_after_empty_placeholder(self) -> None:
        html = """
        <html><head><title>关于学校统一短信平台开通的通知-中国科学技术大学</title></head>
        <body><nav>首页 | 科大新闻 | 学校概况 | 院系介绍</nav>
        <table><tr><td class='title'>关于学校统一短信平台开通的通知</td></tr>
        <tr><td class='content'><div id='vsb_content'><div class='v_news_content'>
        <div class='wp_articlecontent'></div></div></div></td></tr>
        <tr><td><div><p>学校开通统一校园短信平台，为各部门提供信息化支撑服务，并提供发送和管理界面。</p>
        <p>各单位可以联系网络信息中心开通服务，并按要求管理本单位的通讯录和短信配额。</p>
        <img src='/__local/platform.jpg'></div></td></tr></table>
        <footer>Copyright 中国科学技术大学 皖ICP备05002528号</footer></body></html>
        """
        page = extract_page(
            "https://www.ustc.edu.cn/tzggcontent.jsp?urltype=news.NewsContentUrl&wbnewsid=2562",
            html,
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertIn("学校开通统一校园短信平台", page.article.body_text)
        self.assertNotIn("科大新闻", page.article.body_text)
        self.assertNotIn("Copyright", page.article.body_text)
        self.assertEqual(
            [image.url for image in page.article.images],
            ["https://www.ustc.edu.cn/__local/platform.jpg"],
        )

    def test_compact_legacy_footer_nested_in_article_is_removed(self) -> None:
        html = """
        <html><head><title>关于学校统一短信平台开通的通知-中国科学技术大学</title></head><body>
        <div class='wp_articlecontent'>
          <p>为更好向各部门提供信息化支撑服务，学校开通统一校园短信平台，以下为平台开通和使用说明。</p>
          <p><img src='/article-interface.jpg'>平台登录界面和短信发送界面。</p>
          <div class='x1'>Copyright 中国科学技术大学 All Rights Reserved
            <a>联系我们</a><a>皖ICP备05002528号</a>
            <img src='/police-icon.jpg'><span>皖公网安备 34011102001530号</span>
          </div>
        </div></body></html>
        """
        page = extract_page(
            "https://www.ustc.edu.cn/tzggcontent.jsp?urltype=news.NewsContentUrl&wbnewsid=2562",
            html,
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertNotIn("Copyright", page.article.body_text)
        self.assertNotIn("皖ICP备", page.article.body_text)
        self.assertNotIn("皖公网安备", page.article.body_text)
        self.assertEqual(
            [image.url for image in page.article.images],
            ["https://www.ustc.edu.cn/article-interface.jpg"],
        )

    def test_short_authoritative_content_beats_larger_footer_wrapper(self) -> None:
        html = """
        <html><head><title>刘佳月</title></head><body>
        <div class='wp_articlecontent'>负责艺术教学中心和通识教育中心教务工作</div>
        <footer><div class='articlecontent'><div class='content'>网站首页 中心简介 新闻动态
        通知公告 师资队伍 教育教学 演出报告 艺术社团 地址：中国科学技术大学
        Copyright © 2022 中国科学技术大学艺术教学中心 皖ICP备05002528号</div></div></footer>
        </body></html>
        """
        page = extract_page("https://arts.ustc.edu.cn/2022/0617/c31165a559854/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.body_text, "负责艺术教学中心和通识教育中心教务工作")

    def test_word_exported_span_soup_keeps_paragraph_lines(self) -> None:
        html = """
        <html><head><title>体检通知</title></head><body>
        <div class='v_news_content'>
        <p><span>各有关单位：</span></p>
        <p><span>根据省保健委</span><span>《关于做好<span>2026</span>年度健康体检工作的通知》，</span><span>我校现开展年度健康体检工作，</span><span>现将有关事项通知如下，请各单位及时转告相关人员，</span><span>按要求安排好体检预约与车辆乘坐事宜。</span></p>
        <p><span>一、体检对象</span></p>
        <p><span>持有干部保健证的省保健对象人员，及新进的正高级专业技术职务人员（含特任正高），请按通知要求参加。</span></p>
        <p><span>联系电话：</span><span>62283555-800</span><span>或</span><span>802</span><span>。</span></p>
        </div></body></html>
        """
        page = extract_page(
            "https://www.ustc.edu.cn/tzggcontent.jsp?urltype=news.NewsContentUrl&wbnewsid=1&wbtreeid=1363",
            html,
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertIn(
            "根据省保健委《关于做好2026年度健康体检工作的通知》，我校现开展年度健康体检工作，现将有关事项通知如下，请各单位及时转告相关人员，按要求安排好体检预约与车辆乘坐事宜。",
            page.article.body_text,
        )
        self.assertIn("62283555-800或802。", page.article.body_text)

    def test_body_markdown_absolutizes_relative_image_urls(self) -> None:
        html = """
        <html><head><title>校园活动图片报道</title></head><body>
        <div class='v_news_content'>
        <p>学校举办年度校园开放日活动，吸引了众多师生和访客前来参观交流。</p>
        <p><img src='/__local/open-day.jpg' alt='开放日现场'></p>
        </div></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1055/1234.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertIn(
            "![开放日现场](https://www.ustc.edu.cn/__local/open-day.jpg)",
            page.article.body_markdown,
        )

    def test_table_cells_and_rows_are_separated_in_body_text(self) -> None:
        html = """
        <html><head><title>奖学金评选结果公示</title></head><body>
        <div class='v_news_content'>
        <p>现将本年度奖学金评选结果公示如下，公示期为一周，如有异议请联系教务办公室。</p>
        <table><tr><th>项目</th><th>获奖人</th></tr>
        <tr><td>姓名</td><td>张三</td></tr>
        <tr><td>学号</td><td>PB20000001</td></tr></table>
        </div></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1055/1234.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        lines = page.article.body_text.split("\n")
        self.assertIn("项目 获奖人", lines)
        self.assertIn("姓名 张三", lines)
        self.assertIn("学号 PB20000001", lines)

    def test_module_import_does_not_install_global_warning_filter(self) -> None:
        import importlib
        import warnings

        import ustc_crawler.extract as extract_module

        before = list(warnings.filters)
        importlib.reload(extract_module)
        self.assertEqual(
            before,
            warnings.filters,
            "extract must suppress XMLParsedAsHTMLWarning locally, not globally",
        )

    def test_nested_footer_class_is_removed_from_article_container(self) -> None:
        html = """
        <html><head><title>研究生会活动报道</title></head><body>
        <div class='wp_articlecontent'><p>这是足够长的活动报道正文，包含活动时间、地点、参与人员和后续安排。</p>
        <p>第二段介绍活动结果和同学们的反馈意见，属于需要保留的真实正文。</p>
        <div class='footer'><div class='footer_link'>友情链接 中国科学技术大学</div>
        <div class='copyright'>Copyright © 2024 皖ICP备05003562号</div></div></div>
        </body></html>
        """
        page = extract_page("https://gradunion.ustc.edu.cn/2026/0901/c1a2/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertIn("活动结果", page.article.body_text)
        self.assertNotIn("友情链接", page.article.body_text)
        self.assertNotIn("皖ICP备", page.article.body_text)

    def test_detail_title_selector_wins_over_related_article_metadata(self) -> None:
        html = """
        <html><head><meta property='og:title' content='“我曾是那个坐在课堂里的少年。”'></head>
        <body><h2 class='detail_title'>82少班友纪念基金</h2>
        <div class='detail_content fr-view'><div class='wp_articlecontent'>
        1982级校友集体捐资设立，用于支持学院发展。</div></div></body></html>
        """
        page = extract_page("https://sgy.ustc.edu.cn/2026/0713/c42697a747449/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "82少班友纪念基金")
        self.assertEqual(page.article.body_text, "1982级校友集体捐资设立，用于支持学院发展。")

    def test_footer_content_candidate_is_ignored(self) -> None:
        html = """
        <html><head><title>中国科学技术大学就业信息网</title></head><body>
        <header><nav>校园招聘 专场招聘会 通知公告 岗位信息 实习信息</nav></header>
        <article class='news-details'>发布时间： 阅读次数： 上一条： 下一条：</article>
        <footer><div class='articlecontent'>办公地址：中国科学技术大学
        Copyright © 2022 中国科学技术大学就业信息网 皖ICP备05002528号</div></footer>
        </body></html>
        """
        page = extract_page(
            "https://www.job.ustc.edu.cn/Announcement/info.aspx?itemid=8062", html
        )
        self.assertIsNone(page.article)

    def test_body_fallback_keeps_article_with_substantive_paragraphs(self) -> None:
        html = """
        <html><head><title>旧版页面正文</title></head><body>
        <p>这是没有专用正文类名的旧版文章第一段，包含足够完整的公开信息和事项说明。</p>
        <p>这是第二段正文，继续说明办理流程、联系办法和后续安排，不能被误判为空页面。</p>
        </body></html>
        """
        page = extract_page("https://legacy.ustc.edu.cn/info/1/2.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertIn("办理流程", page.article.body_text)

    def test_embedded_pdf_is_article_content_and_a_discovered_link(self) -> None:
        html = """
        <html><head><title>研究生学术论坛获奖名单</title></head><body>
        <div class='wl-con wl-detail'><h1 class='wl-detail-title'>研究生学术论坛获奖名单</h1>
        <span>发布时间：2026-06-02</span><div pdfsrc='/files/winners.pdf'></div></div>
        <footer>Copyright 中国科学技术大学 皖ICP备05002528号</footer></body></html>
        """
        page = extract_page("https://see.ustc.edu.cn/2026/0602/c1a2/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertNotIn("Copyright", page.article.body_text)
        self.assertIn("https://see.ustc.edu.cn/files/winners.pdf", page.links)

    def test_pdf_only_article_container_beats_page_shell(self) -> None:
        html = """
        <html><head><title>新能源会议第二轮通知</title></head><body>
        <div class='col_path'>当前位置：首页 新闻信息 通知公告</div>
        <div class='wp_articlecontent'><p>&nbsp;</p><div pdfsrc='/files/notice.pdf'></div></div>
        <footer>Copyright 中国科学技术大学 皖ICP备05002528号</footer>
        </body></html>
        """
        page = extract_page("https://safetyse.ustc.edu.cn/2026/0509/c4553a1/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertNotIn("当前位置", page.article.body_text)
        self.assertNotIn("Copyright", page.article.body_text)
        self.assertIn("https://safetyse.ustc.edu.cn/files/notice.pdf", page.links)

    def test_video_only_article_container_is_kept_as_an_asset_link(self) -> None:
        html = """
        <html><head><title>校友访谈-中国科学技术大学教育基金会</title></head><body>
        <div class='n_position'>当前位置：首页 &gt; 影像 &gt; 正文</div>
        <section class='show'><div class='show01'><h5>校友访谈</h5></div>
        <div id='vsb_content'><div class='v_news_content'><p>
        <script vurl='/__local/interview.mp4?e=.mp4'>showVsbVideo()</script>
        </p></div></div></section><footer>网站导航和联系地址</footer></body></html>
        """
        page = extract_page("https://ef.ustc.edu.cn/info/1073/2317.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "校友访谈")
        self.assertNotIn("当前位置", page.article.body_text)
        self.assertIn("https://ef.ustc.edu.cn/__local/interview.mp4?e=.mp4", page.links)

    def test_sudy_video_player_keeps_custom_video_container_and_link(self) -> None:
        html = """
        <html><head><title>反诈宣传视频</title></head><body>
        <nav>首页 新闻通知 影像</nav>
        <h1 class='arti_title'>反诈宣传视频</h1>
        <div class='wp_articlecontent'>
          <div class='wp_video_player' sudy-wp-src='/_upload/article/videos/anti-fraud.mp4'></div>
        </div>
        <footer>地址：中国科学技术大学 保卫处</footer></body></html>
        """
        page = extract_page(
            "https://bwc.ustc.edu.cn/2026/0121/c39430a720167/page.htm", html
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "反诈宣传视频")
        self.assertEqual(page.article.body_text, "")
        self.assertNotIn("首页", page.article.body_text)
        self.assertIn(
            "https://bwc.ustc.edu.cn/_upload/article/videos/anti-fraud.mp4", page.links
        )

    def test_generic_banner_detail_shell_is_not_an_article(self) -> None:
        html = """
        <html><head><title>banner3</title></head><body>
        <nav>首页 学校概况 新闻</nav><h1>banner3</h1>
        <div class='content'><p>发布时间：2021-07-05</p></div>
        <footer>Copyright 中国科学技术大学</footer></body></html>
        """
        page = extract_page(
            "https://soe.ustc.edu.cn/2021/0705/c26768a705743/page.htm", html
        )
        self.assertIsNone(page.article)

    def test_scripted_pdf_player_exposes_document_and_preview_images(self) -> None:
        html = """
        <html><head><title>2025年度审计报告</title></head><body>
        <div id='vsb_content'><div class='v_news_content'><p><script>
        var vsb_pdf_image_data = ['/__local/page-1.jpg', '/__local/page-2.jpg'];
        showVsbpdfIframe('/__local/report.pdf', '100%', '600', vsb_pdf_image_data);
        </script></p></div></div><footer>网站导航和联系地址</footer></body></html>
        """
        page = extract_page("https://ef.ustc.edu.cn/info/1022/2374.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(
            [image.url for image in page.article.images],
            [
                "https://ef.ustc.edu.cn/__local/page-1.jpg",
                "https://ef.ustc.edu.cn/__local/page-2.jpg",
            ],
        )
        self.assertIn("https://ef.ustc.edu.cn/__local/report.pdf", page.links)

    def test_legacy_table_article_excludes_breadcrumb_and_repeated_title(self) -> None:
        html = """
        <html><head><title>中国科学技术大学-研究生招生在线</title></head><body>
        <table><tr><td>当前位置-首页-通知公告</td></tr><tr><td>
        <table><tr><td class='bt01'><p>2026年研究生招生录取工作相关通知</p></td></tr>
        <tr><td><p>现将本年度研究生招生录取工作相关安排通知如下，包含录取材料寄送和报到要求。</p></td></tr>
        </table></td></tr></table><footer>招生简章 硕士招生 博士招生</footer></body></html>
        """
        page = extract_page("https://yz1.ustc.edu.cn/article_1190.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "2026年研究生招生录取工作相关通知")
        self.assertNotIn("当前位置", page.article.body_text)
        self.assertNotIn(page.article.title, page.article.body_text)
        self.assertIn("录取材料寄送", page.article.body_text)

    def test_breadcrumb_inside_article_wrapper_is_removed(self) -> None:
        html = """
        <html><head><title>数学学院招生安排</title></head><body>
        <div class='page-content'><div class='breadcrumbs'>当前位置：首页 招生工作</div>
        <p>数学学院公布本年度招生安排，正文包含报名条件、时间节点、材料要求和联系方式。</p></div>
        </body></html>
        """
        page = extract_page("https://math.ustc.edu.cn/2026/0901/c1a2/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertNotIn("当前位置", page.article.body_text)
        self.assertIn("报名条件", page.article.body_text)

    def test_empty_legacy_article_body_does_not_fall_back_to_shell(self) -> None:
        html = """
        <html><head><title>2008年安徽省数学年会照片</title></head><body>
        <nav>首页 新闻资讯 通知</nav><div class='right'><div class='cont'>
        <span class='t'>2008年安徽省数学年会照片</span>
        <p class='time'>发布时间：2008-12-16</p><div class='text'></div>
        </div></div><footer>地址：中国科学技术大学数学系</footer></body></html>
        """
        page = extract_page("https://ahmath.ustc.edu.cn/2019/0416/c1a2/page.htm", html)
        self.assertIsNone(page.article)

    def test_generic_listing_heading_does_not_turn_body_shell_into_article(self) -> None:
        html = """
        <html><head><title>影像</title></head><body>
        <nav>首页 关于我们 新闻通知 新闻 通知 影像 公益捐赠</nav>
        <main><h1>影像</h1><p>统一身份认证 其他账号登录</p></main>
        <footer>中国科学技术大学教育基金会</footer></body></html>
        """
        page = extract_page("https://ef.ustc.edu.cn/info/1073/2317.htm", html)
        self.assertIsNone(page.article)

    def test_blank_legacy_heading_uses_bold_lead_as_title(self) -> None:
        html = """
        <html><head><title>　</title></head><body>
        <div class='wp_articlecontent'><p><strong>物理学院学术交流会</strong></p>
        <p>这是足够长的公开正文内容，用于验证旧模板首段标题可以进入检索索引。</p></div>
        </body></html>
        """
        page = extract_page("https://physics.ustc.edu.cn/2024/0511/c12804a640625/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "物理学院学术交流会")

    def test_english_generic_heading_uses_lead_when_site_title_is_only_branding(self) -> None:
        html = """
        <html><head><title>Lab for Multimodal Biomedical Imaging and Therapy (MBIT)</title></head>
        <body><h2>News</h2><article><p>The Dushu Forum on Medical-Engineering Integration was held at USTC.</p>
        <p>This is a sufficiently long public article body for extraction.</p></article></body></html>
        """
        page = extract_page("https://example.ustc.edu.cn/news/detail/54", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertTrue(page.article.title.startswith("The Dushu Forum"))

    def test_indico_event_keeps_event_heading_without_document_metadata(self) -> None:
        html = """
        <html><head><title>Quark model with hidden local symmetry (5 January 2024) · Indico</title>
        <meta property='og:title' content='Quark model with hidden local symmetry'></head>
        <body><main><h2>Quark model with hidden local symmetry</h2>
        <article><h2>by Speaker Name</h2><p>This is a sufficiently long public event description for extraction, including the complete programme, venue, schedule, and attendance details for interested participants.</p></article>
        </main></body></html>
        """
        page = extract_page("https://indico.pnp.ustc.edu.cn/event/1311/", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "Quark model with hidden local symmetry")

    def test_indico_subpage_ignores_hidden_timezone_widget(self) -> None:
        html = """
        <html><head><title>Event 2026 (12 Oct): Public programme · Indico</title></head>
        <body><article id='tz-selector-widget' style='display: none'>Choose timezone
        Africa/Abidjan Africa/Accra Africa/Addis_Ababa</article>
        <div class='mainContent'><div class='conference-page item-summary'>
        <h1>Public programme</h1><p>公开活动安排正文，包含足够长的文本以便识别为公开详情页面。</p>
        <p>第二段补充活动地点、报告主题和参会说明，避免隐藏控件污染检索内容。</p>
        </div></div></body></html>
        """
        page = extract_page(
            "https://indico.pnp.ustc.edu.cn/event/2026/page/42-programme", html
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "Public programme")
        self.assertNotIn("Choose timezone", page.article.body_text)
        self.assertIn("公开活动安排正文", page.article.body_text)

    def test_date_formats(self) -> None:
        self.assertEqual(parse_date("发布时间：2026年08月21日 12:30"), "2026-08-21T12:30:00")
        self.assertEqual(parse_date("2026/8/2"), "2026-08-02")
        self.assertEqual(parse_date("岗位时间为2026.9-2027.1（5个月）"), "")

    def test_detail_date_from_url_and_heading(self) -> None:
        html = (
            "<html><body><h2>栏目</h2><h2>文章标题</h2><div class='newsNr'>正文</div></body></html>"
        )
        page = extract_page("https://planet.ustc.edu.cn/main/news_detail-20.html", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "文章标题")

    def test_legacy_source_time_is_a_labeled_publication_date(self) -> None:
        html = """
        <html><body><td class='content'><td class='bt01'>旧模板文章</td>
        <p>消息来源： 时间：2022-06-14 14:09:20</p>
        <p>这是足够长的公开正文内容，用来验证旧模板的来源时间字段可以作为发布时间。</p>
        </td></body></html>
        """
        page = extract_page("https://yz1.ustc.edu.cn/article_1190.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2022-06-14T14:09:20")

    def test_empty_detail_shell_is_not_an_article(self) -> None:
        page = extract_page(
            "https://spin.ustc.edu.cn/2024/0221/c35938a631104/page.htm",
            "<html><head><title>Show - Qdiamond</title></head><body></body></html>",
        )
        self.assertIsNone(page.article)

    def test_empty_infobox_detail_shell_is_not_an_article(self) -> None:
        html = """
        <html><head><title>张文真</title></head><body>
        <div class='Header'><div class='MainNav'>网站首页 学院概况 新闻动态</div></div>
        <div class='Main'><div class='MainLeft'><div class='SubMenu'>学院简介 师资队伍</div></div>
        <div class='MainRight'><div class='nTit'>首页 - 党总支组成</div>
        <div class='NewsInfo'><div class='InfoTit'><h1>张文真</h1><p>发布时间：2024-09-04</p></div>
        <div class='InfoBox'></div><div class='NewsBtn'>上一篇： 下一篇：</div></div></div></div>
        </body></html>
        """
        page = extract_page(
            "https://soe.ustc.edu.cn/2024/0904/c36782a652493/page.htm", html
        )
        self.assertIsNone(page.article)

    def test_infobox_content_survives_template_shell_cleanup(self) -> None:
        html = """
        <html><head><title>单位网站</title></head><body>
        <div class='Header'><div class='MainNav'>网站首页 新闻动态</div></div>
        <div class='MainRight'><div class='InfoTit'><h1>环境学院召开工作会议</h1>
        <p>发布时间：2026-09-04</p></div><div class='InfoBox'>
        <p>环境学院召开工作会议，介绍本学期重点工作安排和后续推进计划。</p>
        </div><div class='NewsBtn'>上一篇： 下一篇：</div></div>
        </body></html>
        """
        page = extract_page(
            "https://soe.ustc.edu.cn/2026/0904/c1a2/page.htm", html
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "环境学院召开工作会议")
        self.assertIn("重点工作安排", page.article.body_text)
        self.assertNotIn("网站首页", page.article.body_text)

    def test_lowercase_infobox_removes_legacy_metadata_shell(self) -> None:
        html = """
        <html><head><title>站点</title></head><body>
        <div class='header'><nav>网站首页 新闻</nav></div>
        <div class='infobox'><div class='article'>
        <h1 class='arti_title'>真实标题</h1>
        <p class='arti_metas'><span class='arti_publisher'>发布者：张三</span>
        <span class='arti_update'>发布时间：2026-09-04</span></p>
        <div class='entry'><p>这是足够长的真实文章正文，包含会议安排、事项说明和后续计划，应该保留在正文中。</p></div>
        </div></div></body></html>
        """
        page = extract_page(
            "https://example.ustc.edu.cn/2026/0904/c1a2/page.htm", html
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "真实标题")
        self.assertEqual(page.article.author, "张三")
        self.assertNotIn("发布者", page.article.body_text)
        self.assertNotIn("网站首页", page.article.body_text)
        self.assertIn("后续计划", page.article.body_text)

    def test_empty_news_nr_detail_shell_is_not_an_article(self) -> None:
        html = """
        <html><head><title>深空技术研究院</title></head><body>
        <div class='content'><header>Institute of Deep Space Science and Technology</header>
        <nav>About us News &amp; Events Graduate Programme</nav>
        <div class='newsDe'><div class='mbx'>Home / News /</div>
        <div class='titles'></div><div class='time'>Date:</div><div class='newsNr'></div></div>
        <footer>深空科学技术研究院 Copyright</footer></div>
        </body></html>
        """
        page = extract_page(
            "https://planet.ustc.edu.cn/main/news_detail-19.html", html
        )
        self.assertIsNone(page.article)

    def test_banner_image_only_detail_is_not_an_article(self) -> None:
        html = """
        <html><head><title>banner</title></head><body>
        <div class='infobox'><h1 class='arti_title'>banner</h1>
        <p class='arti_metas'>发布时间：2025-07-03</p>
        <div class='wp_articlecontent'><p><img src='/images/banner.png'></p></div>
        </div></body></html>
        """
        page = extract_page(
            "https://bwc.ustc.edu.cn/2025/0703/c18880a690023/page.htm", html
        )
        self.assertIsNone(page.article)

    def test_image_only_detail_without_title_is_not_an_article(self) -> None:
        html = """
        <html><head><title></title></head><body>
        <div class='wp_articlecontent'><p><img src='/images/notice.png'></p></div>
        <span class='arti_metas'>发布时间：2024-05-11</span>
        </body></html>
        """
        page = extract_page(
            "http://physics.ustc.edu.cn/2024/0511/c12804a640625/page.htm", html
        )
        self.assertIsNone(page.article)

    def test_listing_url_is_not_an_article_with_single_article_tag(self) -> None:
        html = """
        <html><head><title>生命科学与医学部</title></head><body>
        <article><h1>生命科学与医学部</h1>
        <p>行政办公 联系人 负责事务和电话信息，请查看列表内容。</p>
        <p>更多详细资料和办公安排说明。</p></article>
        </body></html>
        """
        page = extract_page("https://biomed.ustc.edu.cn/xrld/list.htm", html)
        self.assertIsNone(page.article)

    def test_paginated_listing_url_is_not_an_article(self) -> None:
        html = """
        <html><head><title>通知公告</title></head><body>
        <article><h1>通知公告</h1>
        <p>通知公告列表包含多条历史通知和分页链接，页面本身不是一条通知。</p>
        <p>请从列表中选择具体的通知详情查看完整内容。</p></article>
        </body></html>
        """
        page = extract_page("https://sts.ustc.edu.cn/tzgg/list10.htm", html)
        self.assertIsNone(page.article)

    def test_vsb_pdf_image_data_script_survives_to_markdown(self) -> None:
        # ef "看图" pages carry the whole article as an image list inside a
        # vsb_pdf_image_data script; the markdown layer turns it into <img>
        # tags, so the script must survive extract-layer script stripping.
        html = """
        <html><head><title>实验室开放日活动图片纪实</title></head><body>
        <div class='v_news_content'><p style='text-indent: 0'>
        <script>var vsb_pdf_image_data = ["/__local/0/13/5F/a.jpg","/__local/4/98/0F/b.jpg"];</script>
        </p></div>
        </body></html>
        """
        page = extract_page("https://ef.ustc.edu.cn/info/1022/2374.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertIn(
            "![](https://ef.ustc.edu.cn/__local/0/13/5F/a.jpg)",
            page.article.body_markdown,
        )
        self.assertIn(
            "![](https://ef.ustc.edu.cn/__local/4/98/0F/b.jpg)",
            page.article.body_markdown,
        )
        # The script source itself must not leak into the plain-text body.
        self.assertNotIn("vsb_pdf_image_data", page.article.body_text)

    def test_single_article_list_htm_page_is_an_article(self) -> None:
        # mcip (a VSB variant) publishes each news item as a single-article
        # column page whose URL ends in list.htm; the page carries a detail
        # title and a full wp_articlecontent body instead of a link list.
        html = """
        <html><head><title>课题组在多模态数据融合方向取得新进展</title></head><body>
        <div class='arti_title'>课题组在多模态数据融合方向取得新进展</div>
        <div class='wp_articlecontent'>
        <p>近日，课题组在多模态数据融合方向取得新进展，相关成果发表于国际学术期刊，受到同行广泛关注。</p>
        <p>该研究提出了一种新的多模态融合框架，显著提升了复杂场景下的感知精度与系统鲁棒性。</p>
        <p>研究工作得到了多个项目的支持，团队成员在数据采集、模型训练和实验验证方面付出了大量努力，并与多家单位开展了深入合作。</p>
        <p>后续工作将围绕实际应用场景展开，持续推进相关成果的转化与落地应用，为行业发展提供有力的技术支撑。</p>
        </div></body></html>
        """
        page = extract_page("https://mcip.ustc.edu.cn/xsjl/list.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertIn("多模态融合框架", page.article.body_text)

    def test_single_article_list_htm_wp_single_variant_is_an_article(self) -> None:
        # Most mcip single-article list.htm pages carry no .arti_title; the
        # title is a bare h2 under the VSB single-article column container
        # (.wp_single #wp_column_article).  53 of 54 saved shell pages have
        # this shape and were all excluded as listings.
        html = """
        <html><head><title>第48届日内瓦国际发明展金奖</title></head><body>
        <h3 class='col_name'>新闻动态</h3>
        <div class='col_news'><div class='col_news_con'><div class='col_news_list'>
        <div class='wp_single wp_column_article' id='wp_column_article'>
        <h2>第48届日内瓦国际发明展金奖</h2>
        <div class='wp_entry'><div class='wp_articlecontent'>
        <p>近日，第48届日内瓦国际发明展在瑞士日内瓦闭幕，并对外公布获奖名单，中国科学技术大学工程科学学院毛磊研究员团队的参展作品荣获金奖。</p>
        <p>该作品提出了一种基于原位磁场感知的锂电池组性能一致性监测方法，显著提升了电池安全管理水平，受到评审专家的高度评价与广泛关注。</p>
        <p>团队成员长期深耕电池管理领域，相关成果已在多个实际场景中得到应用验证，为新能源行业发展提供了有力的技术支撑。</p>
        </div></div></div>
        </div></div></div>
        </body></html>
        """
        page = extract_page("https://mcip.ustc.edu.cn/xsjl_24996/list.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "第48届日内瓦国际发明展金奖")
        self.assertIn("原位磁场感知", page.article.body_text)

    def test_single_article_list_htm_image_only_album_is_an_article(self) -> None:
        # mcip photo-album posts (毕业留念 / 活动图集) are single-article
        # list.htm pages whose wp_articlecontent holds only images; the
        # 180-char body floor must not exclude them.
        html = """
        <html><head><title>2023年毕业留念</title></head><body>
        <div class='wp_single wp_column_article' id='wp_column_article'>
        <h2>2023年毕业留念</h2>
        <div class='wp_entry'><div class='wp_articlecontent'>
        <p><img src='/__local/A/1.jpg' alt='合影'></p>
        <p><img src='/__local/B/2.jpg' alt='毕业照'></p>
        <p><img src='/__local/C/3.jpg' alt='校园'></p>
        </div></div></div>
        </body></html>
        """
        page = extract_page("https://mcip.ustc.edu.cn/2023byln/list.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "2023年毕业留念")
        self.assertEqual(len(page.article.images), 3)

    def test_list_htm_listing_without_article_shell_is_not_an_article(self) -> None:
        # A true VSB listing at */list.htm (no detail title / content
        # container, just a link list) stays excluded.
        html = """
        <html><head><title>新闻动态</title></head><body>
        <ul class='news_list'>
        <li><a href='/xwzx/1.htm'>学院召开年度工作总结会议</a><span>2026-09-01</span></li>
        <li><a href='/xwzx/2.htm'>课题组参加国际学术会议并作报告</a><span>2026-08-20</span></li>
        <li><a href='/xwzx/3.htm'>新生入学教育系列活动顺利开展</a><span>2026-08-05</span></li>
        </ul>
        </body></html>
        """
        page = extract_page("https://mcip.ustc.edu.cn/xwzx/list.htm", html)
        self.assertIsNone(page.article)

    def test_wordpress_attachment_shell_is_not_an_article(self) -> None:
        page = extract_page(
            "https://teach.ustc.edu.cn/?attachment_id=20483",
            """<html><head><meta property='article:published_time' content='2026-08-11T17:30:00'></head>
            <body><article><h1>正在下载，请稍候……</h1><p>通知附件即将开始下载。</p></article></body></html>""",
        )
        self.assertIsNone(page.article)

    def test_detail_date_survives_cleaning_content_root(self) -> None:
        html = """
        <html><body><div class='content'>
        <aside><span id='time'>发布时间：2025-12-02</span></aside>
        <h1>公告标题</h1><p>报名截止日期为2026年9月30日。</p>
        <p>这是足够长的正文内容，用于识别为文章，并避免从正文中的业务日期误判发布时间。</p>
        </div></body></html>
        """
        page = extract_page(
            "https://www.job.ustc.edu.cn/SelectedTrainee/info.aspx?itemid=11317", html
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2025-12-02")

    def test_info_bar_publish_date_wins_over_date_range_in_body(self) -> None:
        html = """
        <html><body>
          <h1>2026-2027学年第一学期课程通知</h1>
          <div class='info-bar'><time>发布时间：2026-07-24</time></div>
          <article><p>岗位时间为2026.9-2027.1（5个月）。</p></article>
        </body></html>
        """
        page = extract_page("https://example.ustc.edu.cn/article/3384", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-07-24")

    def test_unlabelled_schedule_date_is_not_publish_date(self) -> None:
        html = """
        <html><body><article>
          <h1>高新校区班车运行时刻表（2026年8月30日试运行）</h1>
          <p>试运行日期为2026年8月30日，具体班次见正文。</p>
          <p>这是足够长的正文，用于验证事件日期不能被误当成文章发布时间。</p>
        </article></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1029/25469.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "")

    def test_trailing_unlabeled_date_at_body_end(self) -> None:
        html = """
        <html><body><div class='v_news_content'>
        <h1>义诊活动通知</h1>
        <p>免费测血糖、量血压、健康咨询。</p>
        <p style='text-align: right;'>校医院</p>
        <p style='text-align: right;'>2023年11月8日</p>
        </div></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1364/20058.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2023-11-08")

    def test_ustc_notice_content_query_is_an_article(self) -> None:
        html = """
        <html><body>
          <h1>关于开展校园活动的通知</h1>
          <div class='v_news_content'>
            <p>现将本次校园活动的安排通知如下，请相关师生按要求参加。</p>
            <p>具体时间、地点和报名方式请参阅本通知正文及随附材料。</p>
          </div>
        </body></html>
        """
        page = extract_page(
            "https://www.ustc.edu.cn/tzggcontent.jsp?urltype=news.NewsContentUrl&wbtreeid=1364&wbnewsid=25470",
            html,
            source_id="university",
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "关于开展校园活动的通知")

    def test_trailing_labeled_date_at_body_end(self) -> None:
        html = """
        <html><body><article>
          <h1>活动通知</h1>
          <p>具体安排见正文。</p>
          <p style='text-align: right;'>发布时间：2023年11月8日 14:30</p>
        </article></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1364/20059.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2023-11-08T14:30:00")

    def test_article_metadata_and_images(self) -> None:
        html = """
        <html><head><title>旧标题</title>
        <meta name='author' content='张三'>
        <script type='application/ld+json'>
        {"@type":"NewsArticle","headline":"正式标题","datePublished":"2026-08-21T12:30:00","image":"/hero.jpg"}
        </script></head><body><header>logo</header>
        <article><h1>正式标题</h1><p>这是正文。</p><p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章并保留图片。</p>
        <img data-src='/images/a.png' alt='配图'></article><footer>footer</footer></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/2.htm", html, "text/html", "news")
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "正式标题")
        self.assertEqual(page.article.published_at, "2026-08-21T12:30:00")
        self.assertEqual(page.article.author, "张三")
        self.assertEqual(page.article.images[0].url, "https://news.ustc.edu.cn/images/a.png")
        self.assertNotIn("logo", page.article.body_text)

    def test_ustc_news_and_legacy_templates(self) -> None:
        news_html = """
        <html><body><div class='content'>侧栏链接</div>
        <div class='media-foucs'><h1>新闻网标题</h1><span class='date'>2026年08月21日</span>
        <p>新闻网正文。</p><img data-original='/upload/news.jpg' alt='新闻图'></div></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96004.htm", news_html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-08-21")
        self.assertIn("新闻网正文", page.article.body_text)
        self.assertEqual(page.article.images[0].url, "https://news.ustc.edu.cn/upload/news.jpg")

        legacy_html = """
        <html><body><table><tr><td class='content'><h1>旧模板</h1>
        <p>旧模板正文，包含一张图片。</p><img src='/images/old.png'></td></tr></table></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1366/25572.htm", legacy_html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.images[0].url, "https://www.ustc.edu.cn/images/old.png")

    def test_reporter_and_source_signature_at_body_end(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>记者：王敏 来源：中国科学报 2025-11-06 15:07</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96005.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "王敏 / 中国科学报")

    def test_source_only_signature_at_body_end(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>来源：新华网</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96006.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "新华网")

    def test_writer_slash_signature_at_body_end(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>文/张三</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96007.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "张三")

    def test_editor_signature_at_body_end(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>责任编辑：李四</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96008.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "李四")

    def test_reporter_without_colon_at_body_end(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>（记者 黎静）</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96009.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "黎静")

    def test_structured_author_wins_over_body_signature(self) -> None:
        html = """
        <html><head><meta name='author' content=' structured-author '></head>
        <body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>记者：王敏 来源：中国科学报 2025-11-06 15:07</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96010.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "structured-author")

    def test_polluted_meta_author_stops_before_publish_time(self) -> None:
        html = """
        <html><head><meta name='author' content='万宏艳 发布时间：2024-01-02'></head>
        <body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于确认错误拼接的 CMS 作者元数据会被清理。</p>
        </article></body></html>
        """
        page = extract_page("https://math.ustc.edu.cn/2024/0102/c1a2/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "万宏艳")

    def test_metadata_author_stops_before_publish_time(self) -> None:
        html = """
        <html><body><h1>招生复试安排</h1>
        <p class='arti_metas'><span>发布者：黄筑赟</span>
        <span>发布时间：2026-03-19</span></p>
        <article><p>这是足够长的招生复试正文，用于确认元数据字段不会互相污染。</p></article>
        </body></html>
        """
        page = extract_page("https://math.ustc.edu.cn/2026/0319/c1a2/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "黄筑赟")

    def test_source_metadata_does_not_consume_article_body(self) -> None:
        html = """
        <html><body><h1>自动化系招生复试安排</h1>
        <div class='ins-res'><span>来源：自动化系</span>
        <span>发布时间：2026-03-19</span><span>点击：13</span>
        <article><p>这是足够长的招生复试正文，来源字段只能保留机构名称。</p></article>
        </div></body></html>
        """
        page = extract_page("https://auto.ustc.edu.cn/2026/0319/c1a2/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "自动化系")

    def test_regular_content_does_not_trigger_author_extraction(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，其中提到记者王敏曾报道该事件，但没有明确的署名。</p>
        <p>光明日报2018年7月22日</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96011.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "")

    def test_writer_colon_signature_at_body_end(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>文：黄筑赟 图：高华丽</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1047/76512.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "黄筑赟")

    def test_parenthesized_organization_at_body_end(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>（生命科学与医学部）</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1047/79585.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "生命科学与医学部")

    def test_parenthesized_person_is_not_organization_source(self) -> None:
        html = """
        <html><body><article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p>
        <p>（张三）</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1048/96012.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "")

    def test_updated_at_from_jsonld_date_modified(self) -> None:
        html = """
        <html><head><script type='application/ld+json'>
        {"@type":"NewsArticle","headline":"标题","datePublished":"2026-08-21T12:30:00",
         "dateModified":"2026-08-22T09:15:00"}
        </script></head><body><article><h1>标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/2.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.updated_at, "2026-08-22T09:15:00")

    def test_updated_at_from_meta_modified_time(self) -> None:
        html = """
        <html><head><meta property='article:modified_time' content='2026-08-23T16:45:00'>
        <meta property='article:published_time' content='2026-08-21T12:30:00'></head>
        <body><article><h1>标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/3.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.updated_at, "2026-08-23T16:45:00")

    def test_updated_at_from_time_element_with_modification_marker(self) -> None:
        html = """
        <html><body><article><h1>标题</h1>
        <p class='post-meta'>发布时间：2026-08-21 12:30
        <time class='updated' datetime='2026-08-24 10:00'>更新时间：2026-08-24 10:00</time></p>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/4.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.updated_at, "2026-08-24T10:00:00")

    def test_updated_at_from_labeled_update_time(self) -> None:
        html = """
        <html><body><article><h1>标题</h1>
        <div class='info-bar'>发布时间：2026-08-21 12:30 | 更新时间：2026-08-25 14:20</div>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/5.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.updated_at, "2026-08-25T14:20:00")

    def test_category_from_meta_article_section(self) -> None:
        html = """
        <html><head><meta property='article:section' content='学术动态'></head>
        <body><article><h1>标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/6.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.category, "学术动态")

    def test_category_from_breadcrumbs_prefers_deepest_meaningful(self) -> None:
        html = """
        <html><body><nav aria-label='breadcrumb'><a href='/'>首页</a> &gt;
        <a href='/news'>新闻中心</a> &gt; <span>学院新闻</span></nav>
        <article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/7.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.category, "学院新闻")

    def test_category_rejects_only_generic_breadcrumb_items(self) -> None:
        html = """
        <html><body><div class='breadcrumb'><a href='/'>首页</a> &gt; <a href='/news'>新闻中心</a></div>
        <article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/8.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.category, "")

    def test_category_from_first_non_generic_section_heading(self) -> None:
        html = """
        <html><body><h1>通知公告</h1>
        <article><h1>文章标题</h1>
        <p>这是足够长的正文内容，用于让提取器将页面识别为一篇公开文章。</p></article></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1/9.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.category, "通知公告")


class Wave2TitleTests(unittest.TestCase):
    def test_h1_fallback_skips_vsb_column_portlet(self) -> None:
        # hospital.ustc.edu.cn: the first h1 is a VSB column portlet
        # (frag=... + span.Column_Name); the real title is a later bare h1.
        html = """
        <html><head><title>年度健康体检工作通知-医院</title></head><body>
        <h1 class="fl" frag="窗口9"><span class='Column_Name'>医院新闻</span></h1>
        <div class='v_news_content'><h1>年度健康体检工作通知安排</h1>
        <p>各有关单位：现将年度健康体检工作安排通知如下，请各单位及时转告相关人员并按要求预约。</p></div>
        </body></html>
        """
        page = extract_page("https://hospital.ustc.edu.cn/2026/0901/c1234a567890/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "年度健康体检工作通知安排")

    def test_generic_column_heading_falls_back_to_document_title(self) -> None:
        # etcis: portlet h1 carries the column name "中心新闻"; the real
        # title is in <title> with a "标题-站名" shape.
        html = """
        <html><head><title>江苏省南菁高级中学来我校开展暑期研学活动-信息与计算机科学实验教学中心</title></head><body>
        <h1 class="display-4 text-primary">中心新闻</h1>
        <div class='v_news_content'>
        <p>江苏省南菁高级中学师生一行来我校开展暑期研学活动，参观了实验室并听取科普报告。</p></div>
        </body></html>
        """
        page = extract_page("https://etcis-web.ustc.edu.cn/2026/0819/c3668a750644/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "江苏省南菁高级中学来我校开展暑期研学活动")

    def test_detail_heading_notice_title(self) -> None:
        # physics-lab (jxzy): <title> is the site name only, the real title
        # lives in div.notice_title.
        html = """
        <html><head><title>中国科学技术大学物理实验教学中心</title></head><body>
        <div class="notice_title">关于开放物理实验室的通知</div>
        <div class='v_news_content'>
        <p>物理实验室将于下周起面向全校师生开放，请需要使用实验室的老师同学提前预约登记。</p></div>
        </body></html>
        """
        page = extract_page("https://jxzy.ustc.edu.cn/info/1011/1285.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "关于开放物理实验室的通知")

    def test_detail_heading_wl_nrytitle_wins_over_column_h1(self) -> None:
        # quantum-materials: two h1 elements; .wl-stitle h1 is the column
        # name and .wl-nrytitle h1 is the article title.
        html = """
        <html><head><title>陈子元博士毕业欢送会(2025)</title></head><body>
        <div class="wl-stitle"><h1>组内动态</h1></div>
        <div class="wl-nrytitle"><h1>陈子元博士毕业欢送会(2025)</h1></div>
        <div class='v_news_content'>
        <p>课题组为陈子元博士举行毕业欢送会，回顾其在组期间的研究工作并合影留念。</p></div>
        </body></html>
        """
        page = extract_page("https://quantum-materials.ustc.edu.cn/2025/1218/c36591a716747/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "陈子元博士毕业欢送会(2025)")

    def test_detail_heading_detail_header_wins_over_position_box(self) -> None:
        # set: .position-box h1 is the column banner, .detail-header h1 is
        # the real article title.
        html = """
        <html><head><title>从爱因斯坦的好奇心到量子计算机</title></head><body>
        <div class="position-box"><h1>通知公告 - 瀚海讲堂</h1></div>
        <div class="detail-header"><h1>从爱因斯坦的好奇心到量子计算机</h1><p>发布时间：2026-06-09</p></div>
        <div class='v_news_content'>
        <p>瀚海讲堂本期邀请知名学者讲述从爱因斯坦的好奇心到量子计算机的科学历程。</p></div>
        </body></html>
        """
        page = extract_page("https://set.ustc.edu.cn/2026/0609/c35480a743977/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "从爱因斯坦的好奇心到量子计算机")

    def test_sidebar_column_h1_falls_back_to_document_title(self) -> None:
        # hospital.ustc.edu.cn 科室页: the sidebar menu header h1.tit carries
        # the column name "科室设置"; the real title is the article h1 and
        # the document title.
        html = """
        <html><head><title>眼科</title></head><body>
        <div class="sideMenu fl"><div class="side_top"><h1 class="tit">科室设置</h1></div></div>
        <article class="news-details auto w_96"><div><div class="title"><h1>眼科</h1></div>
        <div class='v_news_content'>
        <p>眼科现有医护人员若干名，承担全校师生眼科常见病多发病的诊疗与健康体检工作。</p></div>
        </div></article>
        </body></html>
        """
        page = extract_page("https://hospital.ustc.edu.cn/2023/1120/c35257a620331/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "眼科")

    def test_banner_placeholder_h1_falls_back_to_document_title(self) -> None:
        # hospital.ustc.edu.cn 科室介绍页: the sub-page banner h1 keeps the
        # template placeholder "测试栏目名称"; the real title is the article
        # h1 and the document title.
        html = """
        <html><head><title>院办公室</title></head><body>
        <div class="SubBan"><div class="imgbox"><h1>测试栏目名称</h1></div></div>
        <div class="sideMenu fl"><div class="side_top"><h1 class="tit"></h1></div></div>
        <h1 class="fl" frag="窗口9">科室介绍</h1>
        <article class="news-details auto w_96"><div><div class="title"><h1>院办公室</h1></div>
        <div class='v_news_content'>
        <p>院办公室负责医院行政事务协调、公文流转与会议组织等综合性管理服务工作。</p></div>
        </div></article>
        </body></html>
        """
        page = extract_page("https://hospital.ustc.edu.cn/2023/1215/c35217a624736/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "院办公室")

    def test_lead_paragraph_title_truncates_at_word_boundary(self) -> None:
        # mbit: no title element at all, the lead paragraph becomes the
        # title and must not be cut in the middle of an English word.
        lead = (
            "On June 1, 2026, the research group welcomed Prof. Stefaan from "
            "an international partner institute for a two week academic visit "
            "focused on collaborative research and graduate student training "
            "programs in advanced materials science and engineering."
        )
        html = f"""
        <html><head><title>News</title></head><body>
        <article><p>{lead}</p></article>
        </body></html>
        """
        page = extract_page("https://www.mbit.ustc.edu.cn/news/detail/54", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        title = page.article.title
        self.assertTrue(lead.startswith(title), title)
        self.assertLessEqual(len(title), 160)
        # The character right after the title must be a word boundary.
        self.assertEqual(lead[len(title)], " ", title)

    def test_generic_placeholder_title_uses_bold_lead(self) -> None:
        # spin: every page title is the placeholder "NEW PUBLISHED PAPER";
        # the real paper title is the first <strong> in the body.
        html = """
        <html><head><title>NEW PUBLISHED PAPER</title></head><body>
        <h1 class="wl-newsh1">NEW PUBLISHED PAPER</h1>
        <div class='v_news_content'>
        <p><strong>Quantum sensing with spin defects in wide-bandgap materials</strong></p>
        <p>We report a new study on quantum sensing published this week in a peer reviewed journal.</p>
        </div></body></html>
        """
        page = extract_page("https://spin.ustc.edu.cn/2026/0221/c35808a721553/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(
            page.article.title,
            "Quantum sensing with spin defects in wide-bandgap materials",
        )

    def test_real_title_not_replaced_by_bold_lead(self) -> None:
        html = """
        <html><head><title>正常文章标题-某学院</title></head><body>
        <h1>正常文章标题</h1>
        <div class='v_news_content'>
        <p><strong>加粗的重点句不应成为标题</strong>，正文其余部分正常展开叙述。</p>
        </div></body></html>
        """
        page = extract_page("https://sgy.ustc.edu.cn/2026/0713/c42697a747449/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "正常文章标题")


class Wave2DateTests(unittest.TestCase):
    def test_bare_date_label_is_recognized(self) -> None:
        # university VSB subsites label the date as a bare 日期：... span.
        html = """
        <html><head><title>主题教育专题学习活动通知</title></head><body>
        <div class='v_news_content'>
        <p><span>日期：2024-10-11</span></p>
        <p>现将主题教育专题学习活动安排通知如下，请各支部组织党员按时参加学习。</p>
        </div></body></html>
        """
        page = extract_page(
            "https://www.ustc.edu.cn/tzggcontent.jsp?urltype=news.NewsContentUrl&wbnewsid=2&wbtreeid=1363",
            html,
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2024-10-11")

    def test_event_date_label_is_not_a_publication_date(self) -> None:
        # "比赛日期" / "活动日期" style labels describe the event, not the
        # publication time, and must not satisfy the bare 日期 label.
        html = """
        <html><head><title>校园足球联赛通知</title></head><body>
        <div class='v_news_content'>
        <p>比赛日期：2026-10-01，请各参赛队伍提前半小时到场签到。</p>
        <p>现将校园足球联赛整体安排通知如下，请各单位按要求组织报名工作。</p>
        </div></body></html>
        """
        page = extract_page(
            "https://www.ustc.edu.cn/tzggcontent.jsp?urltype=news.NewsContentUrl&wbnewsid=3&wbtreeid=1363",
            html,
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "")

    def test_labeled_page_date_beats_url_path_date(self) -> None:
        # sjc: the URL path date is the page rebuild date; the 发布时间 in
        # the page header is the real publication date.
        html = """
        <html><head><title>年度审计结果公告</title></head><body>
        <div class='inner-news-detail'>
        <div class='inner-news-hd'>发布时间：2026-05-21 点击率：105 次</div>
        <p>现将年度审计结果公告如下，具体内容请参见附件说明与相关文件材料。</p>
        </div></body></html>
        """
        page = extract_page("https://sjc.ustc.edu.cn/2026/0630/c32882a745899/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-05-21")

    def test_url_path_date_remains_fallback(self) -> None:
        html = """
        <html><head><title>年度审计结果公告</title></head><body>
        <div class='inner-news-detail'>
        <p>现将年度审计结果公告如下，具体内容请参见附件说明与相关文件材料。</p>
        </div></body></html>
        """
        page = extract_page("https://sjc.ustc.edu.cn/2026/0630/c32882a745899/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-06-30")

    def test_time_datetime_attribute_is_used(self) -> None:
        # mbit publication/team pages expose the date only via
        # <time class="entry-date published" datetime="...">.
        html = """
        <html><head><title>Publication - MBIT Lab</title></head><body>
        <article><h1>Some Paper Title</h1>
        <time class="entry-date published" datetime="2026-06-01">June 1, 2026</time>
        <p>Paper abstract content goes here with enough length to be a body of text.</p>
        </article></body></html>
        """
        page = extract_page("https://www.mbit.ustc.edu.cn/publication/detail/100", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-06-01")

    def test_unlabeled_news_top_p_date(self) -> None:
        # xcb: the old template puts a bare date in p.news-top-p above the
        # h5.news-top-h5 title.
        html = """
        <html><head><title>学校召开年度工作会议</title></head><body>
        <p class="news-top-p">2012.12.06</p><h5 class="news-top-h5">学校召开年度工作会议</h5>
        <div class='v_news_content'>
        <p>学校于本周召开年度工作会议，总结全年工作并部署下一阶段重点任务安排。</p>
        </div></body></html>
        """
        page = extract_page("http://xcb.ustc.edu.cn/info/1011/19247.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2012-12-06")

    def test_labeled_date_in_plain_div_near_title(self) -> None:
        # journal: 发布时间 sits in a plain classless div next to the title.
        html = """
        <html><head><title>关于本刊2026年征订的通知</title></head><body>
        <div class="article-head"><h1>关于本刊2026年征订的通知</h1>
        <div>发布时间：2015-11-18</div></div>
        <div class='article-content'>
        <p>本刊2026年度征订工作现已开始，请各单位联系人及时办理相关订阅手续，征订的具体范围、价格与联系方式详见下文说明。</p>
        </div></body></html>
        """
        page = extract_page(
            "https://journal.ustc.edu.cn/ch/reader/view_news.aspx?id=20151118091450623", html
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2015-11-18")


class Wave2BodyTests(unittest.TestCase):
    def test_social_share_visit_count_removed_and_cell_text_kept(self) -> None:
        # sppm old template: the whole body is bare text inside a table
        # cell, mixed with a trailing <p> and share/visit-count widgets.
        html = """
        <html><head><title>评审结果公示</title></head><body>
        <div class='infobox'>
        <div class='social-share'><span class='share_tt'>分享至:</span></div>
        <table><tr><td>现将评审结果公示如下，公示期为五个工作日，如有异议请联系学院办公室反映情况。<span class='WP_VisitCount'>95</span><p>联系人：张老师。</p></td></tr></table>
        </div></body></html>
        """
        page = extract_page("http://sppm.ustc.edu.cn/2012/0521/c13411a269002/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertIn("现将评审结果公示如下", page.article.body_text)
        self.assertIn("联系人：张老师。", page.article.body_text)
        self.assertNotIn("分享至", page.article.body_text)
        self.assertNotIn("分享至", page.article.body_markdown)

    def test_inner_news_header_bar_not_leaked_into_body(self) -> None:
        # sjc: the inner-news-hd bar (发布时间/点击率) must feed the date
        # extraction but never leak into the body.
        html = """
        <html><head><title>年度审计结果公告</title></head><body>
        <div class='inner-news-detail'>
        <div class='inner-news-hd'>发布时间：2026-05-21 点击率：105 次</div>
        <p>现将年度审计结果公告如下，具体内容请参见附件说明与相关文件材料。</p>
        </div></body></html>
        """
        page = extract_page("https://sjc.ustc.edu.cn/2026/0630/c32882a745899/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-05-21")
        self.assertEqual(
            page.article.body_text,
            "现将年度审计结果公告如下，具体内容请参见附件说明与相关文件材料。",
        )
        self.assertNotIn("点击率", page.article.body_markdown)

    def test_wl_post_bar_not_leaked_into_body(self) -> None:
        # nsti / bioinspired: the wl-post bar carries 发布时间 + 访问次数.
        html = """
        <html><head><title>宝钢奖学金评审结果公示</title></head><body>
        <div class='wl-con wl-detail'>
        <div class='wl-detail-title'>宝钢奖学金评审结果公示</div>
        <div class='wl-post'>发布时间：2026-09-01 访问次数：10次</div>
        <p>经评审委员会研究，现将宝钢奖学金评审结果公示如下，公示期为一周。</p>
        </div></body></html>
        """
        page = extract_page(
            "https://bioinspired.sz.ustc.edu.cn/2026/0519/c40763a741141/page.htm", html
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-09-01")
        self.assertNotIn("访问次数", page.article.body_text)
        self.assertNotIn("访问次数", page.article.body_markdown)
        self.assertIn("经评审委员会研究", page.article.body_text)

    def test_weix_time_bar_removed_and_date_read(self) -> None:
        # smile: .central_text wraps a .weix_time meta bar + .center_txt body.
        html = """
        <html><head><title>新生心理健康普查通知</title></head><body>
        <div class='central_text'>
        <div class='weix_time'>发布时间：2026-09-01 发布来源：</div>
        <div class='center_txt'>
        <p>学校将面向全体新生开展心理健康普查，请各学院组织学生按预约时段参加测评。</p>
        </div></div></body></html>
        """
        page = extract_page("http://smile.ustc.edu.cn/index/info/5017", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-09-01")
        self.assertNotIn("发布来源", page.article.body_text)
        self.assertNotIn("发布来源", page.article.body_markdown)
        self.assertIn("心理健康普查", page.article.body_text)

    def test_aside_wrapping_article_is_content(self) -> None:
        # bigdata (Ghost-style): the article lives inside
        # <aside class="col-md-9 sidebar"><article class="post">; the aside
        # must not be treated as a shell container.
        html = """
        <html><head><title>实验室最新研究成果发布</title></head><body>
        <div class='container'><aside class='col-md-9 sidebar'><article class='post'>
        <h1>实验室最新研究成果发布</h1>
        <p>近日实验室在认知智能方向取得重要进展，相关成果已在国际学术会议上正式发表并报告。</p>
        </article></aside></div>
        </body></html>
        """
        page = extract_page("http://bigdata.ustc.edu.cn/class_24/news/news_20241127.html", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "实验室最新研究成果发布")
        self.assertIn("认知智能方向", page.article.body_text)

    def test_aside_wrapping_link_list_keeps_listing_outlinks(self) -> None:
        # bigdata listing pages put the article link table in the same
        # <aside class="col-md-9 sidebar"> main column (no <article> inside);
        # decomposing the aside wiped every outlink, so class_24/class_25
        # article pages never reached the frontier.
        rows = "".join(
            f"<tr><td><span><a href='../class_25/news/news_202507{i:02d}.html'>学术动态新闻标题第{i}条</a></span></td></tr>"
            for i in range(10, 20)
        )
        html = f"""
        <html><head><title>学术动态</title></head><body>
        <section class='content-wrap'>
        <aside class='col-sm-3 sidebar'><a href='../index.html'>首页</a></aside>
        <aside class='col-md-9 sidebar'><div class='widget'>
        <table>{rows}</table>
        </div></aside>
        </section>
        </body></html>
        """
        page = extract_page("https://bigdata.ustc.edu.cn/class_4/news_list_1.html", html)
        news_links = [link for link in page.links if "/class_25/news/" in link]
        self.assertEqual(len(news_links), 10)
        self.assertIn(
            "https://bigdata.ustc.edu.cn/class_25/news/news_20250710.html",
            news_links,
        )


class Wave2AuthorTests(unittest.TestCase):
    def test_author_rejects_date_time_value(self) -> None:
        # jgdw: empty 作者： label followed by the update timestamp; the
        # flattened meta bar must not turn the date into the author.
        html = """
        <html><head><title>机关党委专题学习通知</title></head><body>
        <p class='arti_title'>机关党委专题学习通知</p>
        <p class='arti_metas'><span class='arti_publisher'>作者：</span><span class='arti_update'>2026/07/30 05:48</span><span class='arti_views'>浏览次数：10</span></p>
        <div class='wp_articlecontent'><p>请各党支部组织党员按时参加专题学习，学习内容详见附件材料。</p></div>
        </body></html>
        """
        page = extract_page("https://jgdw.ustc.edu.cn/2026/0730/c19303a749081/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "")

    def test_trailing_source_rejects_funding_prefix(self) -> None:
        # bwc: procurement notices end with 资金来源：自筹, which is not a
        # publication source signature.
        html = """
        <html><head><title>物业服务采购公告</title></head><body>
        <div class='v_news_content'>
        <p>现就校园物业服务项目发布采购公告，欢迎符合条件的供应商参加投标。</p>
        <p>本项目资金来源：自筹。</p>
        </div></body></html>
        """
        page = extract_page("https://bwc.ustc.edu.cn/2026/0717/c5668a747893/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "")

    def test_trailing_writer_rule_rejects_funding_sentence(self) -> None:
        # oic: 本论文/研究/成果受…资助 is an acknowledgement, not a 文/署名.
        html = """
        <html><head><title>国际联合研究成果发表</title></head><body>
        <div class='v_news_content'>
        <p>我校与海外合作高校联合完成的研究成果近日在国际期刊正式发表。</p>
        <p>本论文/研究/成果受中国科学技术大学全球合作拓展培育基金资助。</p>
        </div></body></html>
        """
        page = extract_page("https://oic.ustc.edu.cn/news/19724.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "")

    def test_author_rejects_site_slogan_from_keywords(self) -> None:
        # qybx: the site slogan is filled into 文章来源 and also appears in
        # the keywords meta; such a value is not an author.
        html = """
        <html><head><title>科教融合工作会议召开</title>
        <meta name='keywords' content='中国科学技术大学全院办校所系结合'></head><body>
        <p class='arti_title'>科教融合工作会议召开</p>
        <p class='arti_metas'><span class='arti_from'>文章来源：全院办校所系结合</span><span class='arti_update'>发布时间：2026-08-03</span></p>
        <div class='wp_articlecontent'><p>学校召开科教融合工作会议，部署下一阶段学院与研究所协同重点工作。</p></div>
        </body></html>
        """
        page = extract_page("https://qybx.ustc.edu.cn/2026/0803/c20980a749955/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "")

    def test_author_not_in_keywords_is_preserved(self) -> None:
        html = """
        <html><head><title>科教融合工作会议召开</title>
        <meta name='keywords' content='中国科学技术大学,科教融合'></head><body>
        <p class='arti_title'>科教融合工作会议召开</p>
        <p class='arti_metas'><span class='arti_from'>文章来源：科研部</span><span class='arti_update'>发布时间：2026-08-03</span></p>
        <div class='wp_articlecontent'><p>学校召开科教融合工作会议，部署下一阶段学院与研究所协同重点工作。</p></div>
        </body></html>
        """
        page = extract_page("https://qybx.ustc.edu.cn/2026/0803/c20980a749955/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "科研部")


class Wave2ReviewTests(unittest.TestCase):
    def test_update_time_label_does_not_beat_url_path_date(self) -> None:
        # Regression pin: a page whose only label is 更新时间 must fall back
        # to the URL path date; 更新时间 is a modification time, never the
        # publication date, and must not outrank the path date.
        html = """
        <html><head><title>年度审计结果公告</title></head><body>
        <div class='inner-news-detail'>
        <div class='inner-news-hd'>更新时间：2025-01-15 点击率：88 次</div>
        <p>现将年度审计结果公告如下，具体内容请参见附件说明与相关文件材料。</p>
        </div></body></html>
        """
        page = extract_page("https://sjc.ustc.edu.cn/2026/0630/c32882a745899/page.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.published_at, "2026-06-30")

    def test_sidebar_aside_teaser_not_selected_as_content(self) -> None:
        # Counter-case for the aside relaxation: a real sidebar aside
        # holding a small article teaser must not become the content root
        # when the page has a genuine content container.
        html = """
        <html><head><title>学校召开重要工作部署会议</title></head><body>
        <aside class='sidebar'><article class='post-preview'>
        <h2>相关阅读：另一篇新闻</h2><p>摘要</p></article></aside>
        <div class='v_news_content'><h1>学校召开重要工作部署会议</h1>
        <p>学校于本周召开重要工作部署会议，研究部署下一阶段重点工作任务并提出明确要求。</p></div>
        </body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1055/1234.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, "学校召开重要工作部署会议")
        self.assertIn("研究部署下一阶段重点工作", page.article.body_text)
        self.assertNotIn("相关阅读", page.article.body_text)

    def test_slash_joined_co_authors_are_preserved(self) -> None:
        html = """
        <html><head><title>联合研究成果发布</title></head><body>
        <div class='v_news_content'>
        <p>我校两个课题组联合完成的研究成果近日正式发表，相关工作得到同行关注。</p>
        <p>记者：张三/李四</p>
        </div></body></html>
        """
        page = extract_page("https://news.ustc.edu.cn/info/1055/9999.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "张三/李四")

    def test_meta_author_matching_keywords_is_preserved(self) -> None:
        # Explicit meta/jsonld author is trusted metadata, not a signature
        # heuristic product; the keywords slogan check must not clear it.
        html = """
        <html><head><title>研究中心年度工作进展</title>
        <meta name='author' content='合肥微尺度物质科学国家研究中心'>
        <meta name='keywords' content='中国科学技术大学,合肥微尺度物质科学国家研究中心'></head><body>
        <div class='v_news_content'>
        <p>研究中心发布年度工作进展报告，系统总结各研究方向取得的代表性成果。</p>
        </div></body></html>
        """
        page = extract_page("https://www.ustc.edu.cn/info/1055/1235.htm", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.author, "合肥微尺度物质科学国家研究中心")

    def test_lead_title_with_early_only_space_uses_hard_cut(self) -> None:
        # A lead whose only in-limit space sits right at the start must not
        # shrink the title to a two-word prefix; fall back to the hard cut.
        lead = "On " + "a" * 200
        html = f"""
        <html><head><title>News</title></head><body>
        <article><p>{lead}</p></article>
        </body></html>
        """
        page = extract_page("https://www.mbit.ustc.edu.cn/news/detail/55", html)
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.title, lead[:160])
