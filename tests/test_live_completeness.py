import importlib.util
import json
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

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


LISTING_HTML = """
  <a href="info/1055/96044.htm">新闻</a>
  <a href="info/1055/96045.htm">公告</a>
"""


class MainExitCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "crawler.sqlite"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE articles (url TEXT PRIMARY KEY);
            CREATE TABLE pages (
                url TEXT PRIMARY KEY,
                duplicate_of TEXT,
                canonical_url TEXT,
                access_mode TEXT
            );
            """
        )
        conn.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _index(self, *urls: str) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executemany("INSERT INTO articles(url) VALUES (?)", [(url,) for url in urls])
        conn.commit()
        conn.close()

    def _run_main(self, *argv: str, fetch_html: str = LISTING_HTML) -> int:
        original_fetch = MODULE.fetch
        original_argv = sys.argv
        MODULE.fetch = lambda _url: fetch_html
        sys.argv = ["live_completeness_check.py", "--db", str(self.db_path), *argv]
        try:
            with redirect_stdout(StringIO()):
                return int(MODULE.main())
        finally:
            MODULE.fetch = original_fetch
            sys.argv = original_argv

    def test_source_and_url_must_be_provided_together(self) -> None:
        self.assertEqual(self._run_main("--source", "news"), 2)
        self.assertEqual(self._run_main("--url", "https://news.ustc.edu.cn/"), 2)

    def test_single_source_green_when_every_link_is_indexed(self) -> None:
        self._index(
            "https://news.ustc.edu.cn/info/1055/96044.htm",
            "https://news.ustc.edu.cn/info/1055/96045.htm",
        )
        self.assertEqual(
            self._run_main("--source", "news", "--url", "https://news.ustc.edu.cn/"),
            0,
        )

    def test_single_source_red_when_a_link_is_missing(self) -> None:
        self._index("https://news.ustc.edu.cn/info/1055/96044.htm")
        self.assertEqual(
            self._run_main("--source", "news", "--url", "https://news.ustc.edu.cn/"),
            1,
        )

    def test_single_source_red_when_no_links_extracted(self) -> None:
        self.assertEqual(
            self._run_main(
                "--source",
                "news",
                "--url",
                "https://news.ustc.edu.cn/",
                fetch_html="<html><body><a href='/about.htm'>about</a></body></html>",
            ),
            1,
        )

    def test_default_run_red_when_any_default_source_fails(self) -> None:
        self.assertEqual(self._run_main(), 1)


class DefaultSourceResolutionTests(unittest.TestCase):
    """DEFAULT_SOURCES ids must be resolvable from sources.yaml + discovered units.

    The discovered-units file is generated locally (data/discovered_units.json)
    and is not committed, so these tests build both configs in a temp dir.
    """

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.sources_yaml = root / "sources.yaml"
        self.units_json = root / "discovered_units.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_configs(self, curated_ids: list[str], unit_hosts: list[str]) -> None:
        entries = "\n".join(
            f"  - id: {source_id}\n"
            f"    name: {source_id}\n"
            f"    organization_level: university\n"
            f"    seed_urls: [https://example.test/]\n"
            f"    allowed_hosts: [example.test]"
            for source_id in curated_ids
        )
        self.sources_yaml.write_text(f"sources:\n{entries}\n", encoding="utf-8")
        self.units_json.write_text(
            json.dumps({"units": [{"host": host, "name": host, "url": f"https://{host}/"} for host in unit_hosts]}),
            encoding="utf-8",
        )

    def test_every_default_source_id_resolves(self) -> None:
        self._write_configs(
            curated_ids=["news", "university", "supplemental-po", "supplemental-sie", "supplemental-pnp"],
            unit_hosts=["www.hfnl.ustc.edu.cn", "scms.ustc.edu.cn", "www.nsrl.ustc.edu.cn"],
        )

        resolved = MODULE.configured_source_ids(self.sources_yaml, self.units_json)

        for source_id, _url, _name in MODULE.DEFAULT_SOURCES:
            self.assertIn(source_id, resolved)

    def test_missing_default_source_id_does_not_resolve(self) -> None:
        self._write_configs(
            curated_ids=["news", "university", "supplemental-po", "supplemental-sie"],
            unit_hosts=["www.hfnl.ustc.edu.cn", "scms.ustc.edu.cn", "www.nsrl.ustc.edu.cn"],
        )

        resolved = MODULE.configured_source_ids(self.sources_yaml, self.units_json)

        self.assertNotIn("supplemental-pnp", resolved)

    def test_units_file_is_optional(self) -> None:
        self._write_configs(curated_ids=["news"], unit_hosts=[])
        self.units_json.unlink()

        resolved = MODULE.configured_source_ids(self.sources_yaml, self.units_json)

        self.assertEqual(resolved, {"news"})
