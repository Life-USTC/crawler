import importlib.util
import sqlite3
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "live_completeness_check.py"
SPEC = importlib.util.spec_from_file_location("live_completeness_check", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
article_url_aliases = MODULE.article_url_aliases
audited_unavailable_urls = MODULE.audited_unavailable_urls
extract_news_links = MODULE.extract_news_links
normalize_url = MODULE.normalize_url


class LiveCompletenessTests(unittest.TestCase):
    def test_article_urls_include_scheme_and_duplicate_aliases(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE articles (url TEXT PRIMARY KEY);
            CREATE TABLE pages (url TEXT PRIMARY KEY, duplicate_of TEXT, canonical_url TEXT);
            INSERT INTO articles(url) VALUES ('https://example.ustc.edu.cn/canonical.htm');
            INSERT INTO pages(url,duplicate_of,canonical_url)
            VALUES ('https://example.ustc.edu.cn/alias.htm',
                    'https://example.ustc.edu.cn/canonical.htm', '');
            """
        )

        aliases = article_url_aliases(conn.cursor())
        conn.close()

        self.assertIn("https://example.ustc.edu.cn/canonical.htm", aliases)
        self.assertIn("http://example.ustc.edu.cn/canonical.htm", aliases)
        self.assertIn("https://example.ustc.edu.cn/alias.htm", aliases)

    def test_audited_authentication_gate_is_covered_but_generic_error_is_not(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE pages (url TEXT PRIMARY KEY, access_mode TEXT);
            INSERT INTO pages(url,access_mode)
            VALUES ('https://example.ustc.edu.cn/login.htm','login_required'),
                   ('https://example.ustc.edu.cn/missing.htm','unavailable');
            """
        )

        unavailable = audited_unavailable_urls(conn.cursor())
        conn.close()

        self.assertIn("https://example.ustc.edu.cn/login.htm", unavailable)
        self.assertNotIn("https://example.ustc.edu.cn/missing.htm", unavailable)

    def test_extracts_relative_news_and_notice_links(self) -> None:
        html = """
          <a href="info/1055/96044.htm">新闻</a>
          <a href="/2026/0830/c123a456/page.htm">学院新闻</a>
          <a href="/tzggcontent.jsp?urltype=news.NewsContentUrl&wbtreeid=1001&wbnewsid=42">通知</a>
          <a href="/about.htm">关于</a>
        """

        links = extract_news_links("https://news.ustc.edu.cn/", html)

        self.assertEqual(
            links,
            [
                "https://news.ustc.edu.cn/info/1055/96044.htm",
                "https://news.ustc.edu.cn/2026/0830/c123a456/page.htm",
                "https://news.ustc.edu.cn/tzggcontent.jsp?urltype=news.NewsContentUrl&wbtreeid=1001&wbnewsid=42",
            ],
        )

    def test_source_with_no_extracted_links_is_an_explicit_failure(self) -> None:
        original_fetch = MODULE.fetch
        MODULE.fetch = lambda _url: "<html><body><a href='/about.htm'>about</a></body></html>"
        try:
            result = MODULE.check_source(
                set(),
                {},
                "empty",
                "https://example.ustc.edu.cn/",
                "empty",
                100,
            )
        finally:
            MODULE.fetch = original_fetch

        self.assertEqual(result, (0, 0, False))

    def test_matching_canonicalizes_query_parameter_order(self) -> None:
        live = (
            "https://www.ustc.edu.cn/tzggcontent.jsp?"
            "urltype=news.NewsContentUrl&wbtreeid=1059&wbnewsid=25616"
        )
        stored = (
            "https://www.ustc.edu.cn/tzggcontent.jsp?"
            "urltype=news.NewsContentUrl&wbnewsid=25616&wbtreeid=1059"
        )
        self.assertTrue(normalize_url(live) & normalize_url(stored))
