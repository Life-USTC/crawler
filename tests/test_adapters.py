import unittest

from ustc_crawler import adapters
from ustc_crawler.adapters import adapter_for, register
from ustc_crawler.adapters.base import ArticleFields, SiteAdapter
from ustc_crawler.extract import extract_page

PROBE_HTML = """<html><body><div class="probe-body"><h1>探针标题</h1>
<p>发布时间：2026-09-01</p><div class="probe-content"><p>这是探针正文内容,长度足够二十个字以上。</p></div>
</div></body></html>"""

GENERIC_HTML = """<html><head>
<meta property="og:title" content="通用提取标题">
<meta property="og:description" content="通用提取摘要。">
<meta name="author" content="通用作者">
<meta property="article:section" content="通用栏目">
<meta property="article:published_time" content="2026-08-31">
</head><body>
<div class="wp_articlecontent"><p>这是通用管线提取出的正文内容，长度足够二十个字以上。</p></div>
</body></html>"""


class ProbeAdapter(SiteAdapter):
    name = "probe"
    source_ids = ("probe-source",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        if "probe-content" not in html:
            return None
        return ArticleFields(
            title="探针标题",
            published_at="2026-09-01",
            body_html="<p>这是探针正文内容,长度足够二十个字以上。</p>",
        )


class BoomAdapter(SiteAdapter):
    name = "boom"
    source_ids = ("boom-source",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        raise RuntimeError("boom")


class SparseAdapter(SiteAdapter):
    """Adapter that fills only title/body and leaves metadata fields empty."""

    name = "sparse"
    source_ids = ("sparse-source",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        return ArticleFields(
            title="适配器标题",
            published_at="",
            body_html="<p>这是适配器正文内容，长度足够二十个字以上。</p>",
            author="适配器作者",
        )


class AdapterRegistryTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in ("probe-source", "boom-source", "sparse-source"):
            adapters._by_source.pop(key, None)

    def test_unknown_source_falls_back_to_none(self) -> None:
        self.assertIsNone(adapter_for("news", "https://news.ustc.edu.cn/info/1/2.htm"))

    def test_registered_adapter_matches_source_id(self) -> None:
        register(ProbeAdapter())
        self.assertEqual(adapter_for("probe-source", "https://x.test/1").name, "probe")

    def test_extract_page_prefers_adapter_result(self) -> None:
        register(ProbeAdapter())
        page = extract_page("https://x.test/whatever", PROBE_HTML, source_id="probe-source")
        self.assertIsNotNone(page.article)
        self.assertEqual(page.article.title, "探针标题")
        self.assertEqual(page.article.published_at, "2026-09-01")
        self.assertEqual(page.article.extraction_method, "adapter:probe")

    def test_adapter_none_keeps_generic_result(self) -> None:
        register(ProbeAdapter())
        page = extract_page("https://x.test/no-match", "<html><body><p>x</p></body></html>",
                            source_id="probe-source")
        self.assertIsNone(page.article)

    def test_adapter_exception_falls_back_to_generic_with_log(self) -> None:
        register(BoomAdapter())
        with self.assertLogs("ustc_crawler.extract", level="WARNING") as captured:
            page = extract_page(
                "https://boom.test/info/1/2.htm", GENERIC_HTML, source_id="boom-source"
            )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.extraction_method, "jsonld+meta+heuristic")
        self.assertEqual(page.article.title, "通用提取标题")
        self.assertEqual(page.article.raw_metadata.get("adapter_error"), "boom")
        self.assertTrue(
            any("adapter boom failed" in message for message in captured.output),
            captured.output,
        )

    def test_adapter_empty_fields_fall_back_to_generic_values(self) -> None:
        register(SparseAdapter())
        page = extract_page(
            "https://sparse.test/info/1/2.htm", GENERIC_HTML, source_id="sparse-source"
        )
        self.assertIsNotNone(page.article)
        assert page.article is not None
        self.assertEqual(page.article.extraction_method, "adapter:sparse")
        self.assertEqual(page.article.title, "适配器标题")
        self.assertIn("适配器正文", page.article.body_text)
        # Fields the adapter left empty keep the generic extraction results.
        self.assertEqual(page.article.published_at, "2026-08-31")
        self.assertEqual(page.article.category, "通用栏目")
        self.assertEqual(page.article.summary, "通用提取摘要。")
        # Fields the adapter filled still win over the generic values.
        self.assertEqual(page.article.author, "适配器作者")

    def test_host_conflict_logs_warning(self) -> None:
        class FirstAdapter(SiteAdapter):
            name = "first"
            hosts = ("conflict.test",)

        class SecondAdapter(SiteAdapter):
            name = "second"
            hosts = ("conflict.test",)

        try:
            register(FirstAdapter())
            with self.assertLogs("ustc_crawler.adapters", level="WARNING") as captured:
                register(SecondAdapter())
            self.assertTrue(
                any("conflict.test" in message for message in captured.output),
                captured.output,
            )
            self.assertEqual(
                adapter_for("", "https://conflict.test/info/1/2.htm").name, "second"
            )
        finally:
            adapters._by_host.pop("conflict.test", None)


if __name__ == "__main__":
    unittest.main()
