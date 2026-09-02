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
