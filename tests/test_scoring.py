import unittest
from datetime import date, timedelta

from ustc_crawler.scoring import (
    _date_from_url,
    document_asset_url,
    is_obvious_low_value_url,
    score_page,
    url_priority,
)


class ScoringTests(unittest.TestCase):
    def test_news_is_prioritized_before_generic_pages(self) -> None:
        self.assertGreater(
            url_priority("https://news.ustc.edu.cn/info/1048/96004.htm", "university"),
            url_priority("https://www.ustc.edu.cn/about.htm", "university"),
        )
        self.assertGreater(
            url_priority("https://math.ustc.edu.cn/2026/0818/c1234a567890/page.htm", "unit-math"),
            url_priority("https://math.ustc.edu.cn/about.htm", "unit-math"),
        )

    def test_auth_gate_and_duplicate_are_not_indexable(self) -> None:
        auth = score_page(url="https://example.ustc.edu.cn/login", status=401)
        self.assertEqual((auth.access_mode, auth.value_score), ("auth_required", 0))
        duplicate = score_page(
            url="https://example.ustc.edu.cn/a",
            title="重复页",
            body_text="same",
            duplicate=True,
        )
        self.assertEqual(duplicate.page_kind, "duplicate")
        self.assertLess(duplicate.value_score, 16)

    def test_recent_news_with_body_reaches_full_index_tier(self) -> None:
        result = score_page(
            url="https://news.ustc.edu.cn/info/1048/96004.htm",
            title="学校新闻",
            body_text="正文内容。" * 100,
            has_article=True,
            published_at="2026-08-21",
            link_count=2,
        )
        self.assertEqual(result.page_kind, "news_article")
        self.assertGreaterEqual(result.value_score, 70)
        self.assertEqual(result.value_tier, "full_index")

    def test_repeated_sort_variants_are_filtered(self) -> None:
        low, reason = is_obvious_low_value_url(
            "http://career.lib.ustc.edu.cn/Author/Detail/14429?PageIndex=1&PageSize=10&orderBy=play"
        )
        self.assertTrue(low)
        self.assertIn("variant", reason)

    def test_uploaded_html_attachment_is_filtered(self) -> None:
        low, reason = is_obvious_low_value_url(
            "http://scc.ustc.edu.cn/_upload/article/files/7d/f9/"
            "033cd3b84a9d8a16b2b2eb9987e6/W020150417520333865223.htm"
        )
        self.assertTrue(low)
        self.assertEqual(reason, "uploaded HTML attachment")
        self.assertEqual(
            is_obvious_low_value_url(
                "http://scc.ustc.edu.cn/2009/1014/c396a3060/page.htm"
            ),
            (False, ""),
        )

    def test_cms_download_endpoint_is_a_document_asset(self) -> None:
        self.assertTrue(
            document_asset_url(
                "https://www.ustc.edu.cn/system/_content/download.jsp?urltype=news.DownloadAttachUrl&wbfileid=12084527"
            )
        )

    def test_notice_id_is_not_mistaken_for_session_id(self) -> None:
        notice = (
            "https://www.ustc.edu.cn/tzggcontent.jsp?"
            "urltype=news.NewsContentUrl&wbnewsid=25616&wbtreeid=1059"
        )
        self.assertEqual(is_obvious_low_value_url(notice), (False, ""))
        self.assertEqual(
            is_obvious_low_value_url("https://example.ustc.edu.cn/page?sid=secret"),
            (True, "session-specific URL"),
        )

    def test_indico_ui_variants_are_filtered(self) -> None:
        for url in (
            "https://indico.pnp.ustc.edu.cn/event/4883/event.ics",
            "https://indico.pnp.ustc.edu.cn/event/4883/?view=standard_numbered",
            "https://indico.pnp.ustc.edu.cn/event/4883/contributions/29439/author/1059",
            "https://indico.pnp.ustc.edu.cn/category/0/overview?date=2026-08-25&period=day",
        ):
            low, reason = is_obvious_low_value_url(url)
            self.assertTrue(low, url)
            self.assertIn("Indico", reason)

    def test_recent_publication_date_boosts_priority(self) -> None:
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=400)).isoformat()
        base = url_priority("https://news.ustc.edu.cn/info/1048/96004.htm", "news")
        recent = url_priority(
            "https://news.ustc.edu.cn/info/1048/96004.htm", "news", published_at=today
        )
        stale = url_priority(
            "https://news.ustc.edu.cn/info/1048/96004.htm", "news", published_at=old
        )
        self.assertGreater(recent, base)
        self.assertEqual(stale, base)

    def test_url_path_date_is_used_for_recency_boost(self) -> None:
        today = date.today()
        recent_path = f"https://math.ustc.edu.cn/{today:%Y/%m%d}/c1234a567890/page.htm"
        old_path = "https://math.ustc.edu.cn/2020/0115/c1234a567890/page.htm"
        self.assertTrue(_date_from_url(recent_path))
        self.assertTrue(_date_from_url(old_path))
        self.assertGreater(
            url_priority(recent_path, "unit-math"),
            url_priority(old_path, "unit-math"),
        )

    def test_recent_document_attachment_gets_lower_priority_than_article(self) -> None:
        today = date.today().isoformat()
        doc = url_priority(
            "https://www.ustc.edu.cn/system/_content/download.jsp?urltype=news.DownloadAttachUrl&wbfileid=1",
            "university",
            published_at=today,
        )
        article = url_priority(
            "https://news.ustc.edu.cn/info/1048/96004.htm", "news", published_at=today
        )
        self.assertGreater(article, doc)
