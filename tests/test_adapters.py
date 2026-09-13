import unittest

from ustc_crawler.adapters import adapter_for, register
from ustc_crawler.adapters.base import ArticleFields, SiteAdapter
from ustc_crawler.extract import extract_page

PROBE_HTML = """<html><body><div class="probe-body"><h1>探针标题</h1>
<p>发布时间：2026-09-01</p><div class="probe-content"><p>这是探针正文内容,长度足够二十个字以上。</p></div>
</div></body></html>"""


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


class AdapterRegistryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
