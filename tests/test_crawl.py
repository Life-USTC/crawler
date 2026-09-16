import asyncio
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support import store_core
from ustc_crawler.crawl import (
    AsyncCrawler,
    CrawlOptions,
    _is_after_since,
    _is_html,
    _parse_since,
)
from ustc_crawler.models import ArticleDocument, FetchResponse, ImageRef, PageDocument, SourceConfig
from ustc_crawler.store import Store


class SinceHelpersTests(unittest.TestCase):
    def test_parse_since_rejects_invalid_date(self) -> None:
        self.assertIsNone(_parse_since("not-a-date"))
        self.assertIsNone(_parse_since(""))

    def test_parse_since_keeps_explicit_timezone(self) -> None:
        parsed = _parse_since("2026-01-01T00:00:00+05:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=5))

    def test_parse_since_fills_local_timezone_only_when_naive(self) -> None:
        parsed = _parse_since("2026-01-01")
        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo)

    def test_is_after_since_handles_missing_or_invalid_dates(self) -> None:
        since = _parse_since("2025-01-01")
        self.assertTrue(_is_after_since("", since))
        self.assertTrue(_is_after_since("invalid", since))

    def test_binary_payload_is_not_html_even_when_mislabeled(self) -> None:
        self.assertFalse(
            _is_html(
                "text/html",
                "https://example.ustc.edu.cn/download",
                b"PK\x03\x04office document",
            )
        )


class IncrementalResetTests(unittest.TestCase):
    def test_reset_revisits_shallow_listings_but_not_deep_archive_pages(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            source = SourceConfig(
                id="news",
                name="测试新闻",
                organization_level="university",
                seed_urls=["https://news.example.test/"],
                allowed_hosts=["news.example.test"],
            )
            store.add_source(source)
            urls = {
                "seed": ("https://news.example.test/", 0),
                "shallow": ("https://news.example.test/channel/1.htm", 3),
                "deep": ("https://news.example.test/channel/100.htm", 20),
                "missing_sitemap": ("https://news.example.test/sitemap.xml", 0),
                "stale_seed": ("https://news.example.test/main.htm", 0),
            }
            for url, depth in urls.values():
                parent = urls["seed"][0] if url == urls["missing_sitemap"][0] else ""
                store.enqueue(url, "news", depth, parent, 500)
                store.save_page(
                    PageDocument(
                        requested_url=url,
                        final_url=url,
                        status=200,
                        content_type="text/html",
                        fetched_at="2026-08-01T00:00:00+08:00",
                        title="列表",
                        canonical_url=url,
                        html=f"<html><body>{url}</body></html>",
                        links=[],
                        images=[],
                        page_kind=(
                            "inaccessible"
                            if url
                            in {
                                urls["missing_sitemap"][0],
                                urls["stale_seed"][0],
                            }
                            else "news_listing"
                        ),
                    ),
                    "news",
                    depth,
                )
                store.mark_done(url)
            store.mark_done(urls["missing_sitemap"][0], "http 404")
            store.mark_done(urls["stale_seed"][0], "http 404")

            # Simulate an interrupted older incremental run.
            store_core(store).execute(
                "UPDATE frontier SET status='pending' WHERE url=?",
                (urls["deep"][0],),
            )
            store_core(store).commit()

            store.reset_seeds_and_listings(
                {
                    "news": {
                        urls["seed"][0],
                    }
                }
            )
            statuses = {
                row["url"]: row["status"]
                for row in store_core(store).execute("SELECT url,status FROM frontier")
            }
            store.close()

        self.assertEqual(statuses[urls["seed"][0]], "pending")
        self.assertEqual(statuses[urls["shallow"][0]], "pending")
        self.assertEqual(statuses[urls["deep"][0]], "done")
        self.assertEqual(statuses[urls["missing_sitemap"][0]], "error")
        self.assertEqual(statuses[urls["stale_seed"][0]], "error")


class CrawlSinceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.db_path = self.data_dir / "crawler.sqlite"
        self.store = Store(self.db_path, self.data_dir)
        self.store.add_source(
            SourceConfig(
                id="news",
                name="测试新闻",
                organization_level="university",
                seed_urls=["https://news.example.test/"],
                allowed_hosts=["news.example.test"],
            )
        )
        self.store.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _crawler(self, since: str = "") -> AsyncCrawler:
        options = CrawlOptions(
            db_path=str(self.db_path),
            data_dir=str(self.data_dir),
            since=since,
            news_first=True,
        )
        crawler = AsyncCrawler(options)
        crawler.sources = {
            "news": SourceConfig(
                id="news",
                name="测试新闻",
                organization_level="university",
                seed_urls=["https://news.example.test/"],
                allowed_hosts=["news.example.test"],
            )
        }
        crawler.enqueued = set()
        return crawler

    async def test_seed_queue_skips_urls_older_than_since(self) -> None:
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        old = (date.today() - timedelta(days=60)).isoformat()
        new = date.today().isoformat()
        store = Store(self.db_path, self.data_dir)
        store.enqueue("https://news.example.test/old/article.htm", "news", 1, "parent", 100)
        store.enqueue("https://news.example.test/new/article.htm", "news", 1, "parent", 100)
        store.save_article_hint("https://news.example.test/old/article.htm", old, "parent")
        store.save_article_hint("https://news.example.test/new/article.htm", new, "parent")
        store.close()

        crawler = self._crawler(since=cutoff)
        await crawler._seed_queue()
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        statuses = {
            row["url"]: row["status"]
            for row in store_core(store).execute("SELECT url, status FROM frontier").fetchall()
        }
        self.assertEqual(statuses["https://news.example.test/old/article.htm"], "filtered")
        self.assertEqual(statuses["https://news.example.test/new/article.htm"], "pending")
        store.close()

    async def test_seed_queue_applies_max_depth_to_persisted_frontier(self) -> None:
        url = "https://news.example.test/archive/deep.htm"
        store = Store(self.db_path, self.data_dir)
        store.enqueue(url, "news", 7, "https://news.example.test/archive/", 100)
        store.close()

        crawler = self._crawler()
        crawler.options.max_depth = 6
        await crawler._seed_queue()
        self.assertTrue(crawler.queue.empty())
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
            "SELECT status,last_error FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(row["status"], "filtered")
        self.assertEqual(row["last_error"], "depth 7 exceeds max depth 6")
        store.close()

    async def test_incremental_seed_queue_skips_existing_non_publication_page(self) -> None:
        url = "https://news.example.test/course/resource.htm"
        store = Store(self.db_path, self.data_dir)
        store.enqueue(url, "news", 2, "https://news.example.test/", 100)
        store.save_page(
            PageDocument(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                fetched_at="2026-08-01T00:00:00+08:00",
                title="课程资料",
                canonical_url=url,
                html="<html><body>课程资料</body></html>",
                links=[],
                images=[],
                page_kind="course_resource",
                access_mode="public",
                value_score=40,
            ),
            "news",
            2,
        )
        store_core(store).execute("UPDATE frontier SET status='pending' WHERE url=?", (url,))
        store_core(store).commit()
        store.close()

        crawler = self._crawler()
        crawler.options.incremental = True
        await crawler._seed_queue()
        self.assertTrue(crawler.queue.empty())
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
            "SELECT status,last_error FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(row["status"], "filtered")
        self.assertEqual(row["last_error"], "incremental skip: existing course_resource page")
        store.close()

    async def test_enqueue_skips_old_article_hint(self) -> None:
        cutoff = date.today().isoformat()
        old = (date.today() - timedelta(days=1)).isoformat()
        store = Store(self.db_path, self.data_dir)
        store.enqueue("https://news.example.test/old/article.htm", "news", 1, "parent", 100)
        store.save_article_hint("https://news.example.test/old/article.htm", old, "parent")
        store.close()

        crawler = self._crawler(since=cutoff)
        source = crawler.sources["news"]
        await crawler._enqueue("https://news.example.test/old/article.htm", source, 1, "parent")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
            "SELECT status FROM frontier WHERE url=?",
            ("https://news.example.test/old/article.htm",),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "filtered")
        store.close()

    async def test_incremental_enqueue_skips_undated_deep_link(self) -> None:
        url = "https://news.example.test/archive/article.htm"
        store = Store(self.db_path, self.data_dir)
        store.enqueue(url, "news", 5, "https://news.example.test/archive/list.htm", 500)
        store.close()
        crawler = self._crawler()
        crawler.options.incremental = True
        crawler.source_since = {"news": _parse_since(date.today().isoformat())}
        source = crawler.sources["news"]

        await crawler._enqueue(
            url,
            source,
            5,
            "https://news.example.test/archive/list.htm",
        )
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
            "SELECT status,last_error FROM frontier WHERE url=?",
            (url,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "filtered")
        self.assertEqual(row["last_error"], "incremental skip: undated deep link")
        store.close()

    async def test_incremental_new_source_also_skips_undated_deep_link(self) -> None:
        url = "https://news.example.test/archive/deep.htm"
        crawler = self._crawler()
        crawler.options.incremental = True

        await crawler._enqueue(
            url,
            crawler.sources["news"],
            5,
            "https://news.example.test/archive/list.htm",
        )
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
            "SELECT 1 FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertIsNone(row)
        store.close()

    async def test_seed_queue_filters_uploaded_html_attachments(self) -> None:
        url = (
            "https://news.example.test/_upload/article/files/7d/f9/"
            "033cd3b84a9d8a16b2b2eb9987e6/W020150417520333865223.htm"
        )
        store = Store(self.db_path, self.data_dir)
        store.enqueue(url, "news", 2, "https://news.example.test/article/1", 500)
        store.close()

        crawler = self._crawler()
        await crawler._seed_queue()
        self.assertTrue(crawler.queue.empty())
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
            "SELECT status,last_error FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "filtered")
        self.assertEqual(row["last_error"], "uploaded HTML attachment")
        store.close()

    async def test_url_date_wins_over_stale_listing_hint(self) -> None:
        url = "https://news.example.test/2026/0830/c1a2/page.htm"
        store = Store(self.db_path, self.data_dir)
        store.save_article_hint(url, "2026-08-18", "https://news.example.test/list.htm")
        store.close()

        crawler = self._crawler()
        crawler.options.incremental = True
        crawler.source_since = {"news": _parse_since("2026-08-30")}
        await crawler._enqueue(
            url,
            crawler.sources["news"],
            2,
            "https://news.example.test/list.htm",
        )
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
            "SELECT status,last_error FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertFalse(row["last_error"])
        store.close()

    async def test_process_does_not_save_article_older_than_since(self) -> None:
        cutoff = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        html = f"""<html><body><article>
          <h1>昨日新闻</h1>
          <div class='info-bar'><time>发布时间：{yesterday}</time></div>
          <p>这是足够长的正文内容，用来验证早于 --since 的文章不会被写入 articles 表。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
        </article></body></html>"""

        crawler = self._crawler(since=cutoff)

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process("https://news.example.test/article/1", "news", 0, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        article = store_core(store).execute(
            "SELECT url FROM articles WHERE url=?",
            ("https://news.example.test/article/1",),
        ).fetchone()
        self.assertIsNone(article)
        store.close()

    async def test_process_saves_recent_article_when_since_is_set(self) -> None:
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        recent = (date.today() - timedelta(days=1)).isoformat()
        html = f"""<html><body><article>
          <h1>近日新闻</h1>
          <div class='info-bar'><time>发布时间：{recent}</time></div>
          <p>这是足够长的正文内容，用来验证晚于 --since 的文章仍然会被正常写入 articles 表。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
        </article></body></html>"""

        crawler = self._crawler(since=cutoff)

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process("https://news.example.test/article/2", "news", 0, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        article = store_core(store).execute(
            "SELECT url FROM articles WHERE url=?",
            ("https://news.example.test/article/2",),
        ).fetchone()
        self.assertIsNotNone(article)
        store.close()

    async def test_process_saves_unseen_shallow_gap_older_than_incremental_cutoff(self) -> None:
        cutoff = date.today()
        yesterday = (cutoff - timedelta(days=1)).isoformat()
        html = f"""<html><body><article>
          <h1>增量截止日前的新闻</h1>
          <div class='info-bar'><time>发布时间：{yesterday}</time></div>
          <p>这是足够长的正文内容，用来验证按来源计算的增量截止日期会在最终保存时生效。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
        </article></body></html>"""

        crawler = self._crawler()
        crawler.source_since = {"news": _parse_since(cutoff.isoformat())}

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process("https://news.example.test/article/3", "news", 0, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        article = store_core(store).execute(
            "SELECT url FROM articles WHERE url=?",
            ("https://news.example.test/article/3",),
        ).fetchone()
        self.assertIsNotNone(article)
        store.close()

    async def test_incremental_refetch_refreshes_old_article_without_expanding_archive(self) -> None:
        cutoff = date.today()
        yesterday = (cutoff - timedelta(days=1)).isoformat()
        url = "https://news.example.test/article/4"
        archive_url = "https://news.example.test/archive/related.htm"
        store = Store(self.db_path, self.data_dir)
        store.save_article(
            ArticleDocument(
                url=url,
                source_id="news",
                title="原始标题",
                author="",
                published_at=yesterday,
                updated_at="",
                category="",
                summary="",
                body_html="<p>原始正文</p>",
                body_text="原始正文",
                body_markdown="原始正文",
                extraction_method="test",
                source_page_url=url,
            )
        )
        store.close()
        html = f"""<html><body><article>
          <h1>刷新后的旧标题</h1>
          <div class='info-bar'><time>发布时间：{yesterday}</time></div>
          <p>这是足够长的正文内容，用来验证增量重访不会删除或者覆盖已有的旧文章。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
          <a href='{archive_url}'>相关文章</a>
        </article></body></html>"""
        crawler = self._crawler()
        crawler.options.incremental = True
        crawler.source_since = {"news": _parse_since(cutoff.isoformat())}

        async def fake_fetch(requested_url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=requested_url,
                final_url=requested_url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process(url, "news", 1, "https://news.example.test/")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        article = store_core(store).execute(
            "SELECT title FROM articles WHERE url=?", (url,)
        ).fetchone()
        related = store_core(store).execute(
            "SELECT 1 FROM frontier WHERE url=?", (archive_url,)
        ).fetchone()
        self.assertIsNotNone(article)
        self.assertEqual(article["title"], "刷新后的旧标题")
        self.assertIsNone(related)
        store.close()

    async def test_requested_url_date_survives_redirect_to_undated_url(self) -> None:
        requested_url = "https://news.example.test/2026/0830/c1a2/page.htm"
        final_url = "https://news.example.test/article/latest.htm"
        html = """<html><body><article>
          <h1>重定向新闻</h1>
          <p>这是足够长的正文内容，用来验证请求地址中编码的日期在重定向后仍被保留。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
        </article></body></html>"""
        crawler = self._crawler()

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=final_url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process(requested_url, "news", 1, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        article = store_core(store).execute(
            "SELECT published_at FROM articles WHERE url=?", (final_url,)
        ).fetchone()
        page = store_core(store).execute(
            "SELECT published_at FROM pages WHERE url=?", (requested_url,)
        ).fetchone()
        self.assertIsNotNone(article)
        self.assertEqual(article["published_at"], "2026-08-30")
        self.assertEqual(page["published_at"], "2026-08-30")
        store.close()

    async def test_shell_listing_with_article_links_is_followed(self) -> None:
        # A JavaScript shell listing carries no text body, so the scorer
        # honestly reports shell/0; the crawler must still follow it when the
        # page exposes enough same-host article-shaped links.
        grad_source = SourceConfig(
            id="grad",
            name="研究生院示例",
            organization_level="university",
            seed_urls=["https://yz.sample.cn/"],
            allowed_hosts=["yz.sample.cn"],
        )
        store = Store(self.db_path, self.data_dir)
        store.add_source(grad_source)
        store.close()

        links_html = "".join(
            f"<a href='https://yz.sample.cn/info/1055/{1000 + index}.htm'>详情{index}</a>"
            for index in range(6)
        )
        html = f"<html><body><div class='list'><ul>{links_html}</ul></div></body></html>"
        crawler = self._crawler()
        crawler.sources["grad"] = grad_source
        crawler.configured_sources["grad"] = grad_source

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process("https://yz.sample.cn/column/181", "grad", 1, "https://yz.sample.cn/")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        page_row = store_core(store).execute(
            "SELECT page_kind, value_score FROM pages WHERE url=?",
            ("https://yz.sample.cn/column/181",),
        ).fetchone()
        frontier = {
            row["url"]: row["status"]
            for row in store_core(store).execute(
                "SELECT url, status FROM frontier WHERE source_id='grad'"
            ).fetchall()
        }
        store.close()

        self.assertIsNotNone(page_row)
        self.assertEqual(page_row["page_kind"], "shell")
        for index in range(6):
            article_url = f"https://yz.sample.cn/info/1055/{1000 + index}.htm"
            self.assertEqual(frontier.get(article_url), "pending")

    async def test_small_identical_bodies_across_hosts_are_not_duplicates(self) -> None:
        # Tiny stub pages (redirect placeholders, empty shells) collide by
        # digest across unrelated hosts; they must not suppress each other.
        stub_sources = [
            SourceConfig(
                id="sta",
                name="站点甲",
                organization_level="university",
                seed_urls=["https://a.sample.cn/"],
                allowed_hosts=["a.sample.cn"],
            ),
            SourceConfig(
                id="stb",
                name="站点乙",
                organization_level="university",
                seed_urls=["https://b.sample.cn/"],
                allowed_hosts=["b.sample.cn"],
            ),
        ]
        store = Store(self.db_path, self.data_dir)
        for stub_source in stub_sources:
            store.add_source(stub_source)
        store.close()

        html = (
            "<html><head><meta http-equiv='refresh' content='0;url=/main.htm'></head>"
            "<body>页面跳转中</body></html>"
        )
        crawler = self._crawler()
        for stub_source in stub_sources:
            crawler.sources[stub_source.id] = stub_source
            crawler.configured_sources[stub_source.id] = stub_source

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process("https://a.sample.cn/", "sta", 0, "")
        await crawler._process("https://b.sample.cn/", "stb", 0, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        rows = {
            row["url"]: row["duplicate_of"]
            for row in store_core(store).execute("SELECT url, duplicate_of FROM pages")
        }
        store.close()

        self.assertEqual(rows["https://a.sample.cn/"], "")
        self.assertEqual(rows["https://b.sample.cn/"], "")

    async def test_duplicate_page_still_contributes_outlinks(self) -> None:
        # A duplicate page is suppressed from the article index, but its
        # outlinks are still discovery evidence and must reach the frontier.
        dup_source = SourceConfig(
            id="dup",
            name="重复示例站",
            organization_level="university",
            seed_urls=["https://dup.sample.cn/"],
            allowed_hosts=["dup.sample.cn"],
        )
        keeper_url = "https://dup.sample.cn/info/1055/1001.htm"
        duplicate_url = "https://dup.sample.cn/info/1055/1002.htm"
        outlink = "https://dup.sample.cn/info/1055/2000.htm"
        padding = "相同正文填充段落，用来让页面体超过小页面判重下限。" * 100
        html = f"""<html><body><article>
          <h1>跨栏目重复新闻</h1>
          <p>{padding}</p>
          <a href='{outlink}'>相关阅读</a>
        </article></body></html>"""
        body = html.encode("utf-8")
        self.assertGreaterEqual(len(body), 2048)

        store = Store(self.db_path, self.data_dir)
        store.add_source(dup_source)
        store.save_page(
            PageDocument(
                requested_url=keeper_url,
                final_url=keeper_url,
                status=200,
                content_type="text/html",
                fetched_at="2026-08-01T00:00:00+08:00",
                title="跨栏目重复新闻",
                canonical_url=keeper_url,
                html=html,
                links=[outlink],
                images=[],
            ),
            "dup",
            1,
        )
        store.save_article(
            ArticleDocument(
                url=keeper_url,
                source_id="dup",
                title="跨栏目重复新闻",
                author="",
                published_at="",
                updated_at="",
                category="",
                summary="",
                body_html=f"<p>{padding}</p>",
                body_text=padding,
                body_markdown=padding,
                extraction_method="test",
                source_page_url=keeper_url,
            )
        )
        store.close()

        crawler = self._crawler()
        crawler.sources["dup"] = dup_source
        crawler.configured_sources["dup"] = dup_source

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=body,
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process(duplicate_url, "dup", 1, "https://dup.sample.cn/column/2.htm")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        page_row = store_core(store).execute(
            "SELECT duplicate_of FROM pages WHERE url=?", (duplicate_url,)
        ).fetchone()
        keeper_article = store_core(store).execute(
            "SELECT url FROM articles WHERE url=?", (keeper_url,)
        ).fetchone()
        duplicate_article = store_core(store).execute(
            "SELECT url FROM articles WHERE url=?", (duplicate_url,)
        ).fetchone()
        frontier_row = store_core(store).execute(
            "SELECT status FROM frontier WHERE url=?", (outlink,)
        ).fetchone()
        store.close()

        self.assertEqual(page_row["duplicate_of"], keeper_url)
        self.assertIsNotNone(keeper_article)
        self.assertIsNone(duplicate_article)
        self.assertIsNotNone(frontier_row)
        self.assertEqual(frontier_row["status"], "pending")

    def _dup_source(self) -> SourceConfig:
        return SourceConfig(
            id="dup",
            name="重复示例站",
            organization_level="university",
            seed_urls=["https://dup.sample.cn/"],
            allowed_hosts=["dup.sample.cn"],
        )

    def _article_html(self, column: str, body_text: str) -> str:
        return f"""<html><body><nav>{column}栏目导航，用来让两个栏目的原始页面字节不同。</nav>
          <article>
            <h1>栏目同文标题</h1>
            <p>{body_text}</p>
          </article>
        </body></html>"""

    async def _process_column_pages(self, pages: dict[str, str]) -> None:
        dup_source = self._dup_source()
        store = Store(self.db_path, self.data_dir)
        store.add_source(dup_source)
        store.close()

        crawler = self._crawler()
        crawler.sources["dup"] = dup_source
        crawler.configured_sources["dup"] = dup_source

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=pages[url].encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        for url in pages:
            await crawler._process(url, "dup", 1, "https://dup.sample.cn/column/1.htm")
        await crawler.close()

    async def test_same_body_articles_across_columns_are_deduplicated(self) -> None:
        # VSB sites republish the same article under several columns with
        # distinct URLs and page chrome; identical body text must collapse to
        # the first indexed copy.
        first_url = "https://dup.sample.cn/info/1055/1001.htm"
        second_url = "https://dup.sample.cn/info/1055/1002.htm"
        body_text = "跨栏目同文正文，用来验证按内容哈希去重。" * 20
        await self._process_column_pages(
            {
                first_url: self._article_html("甲", body_text),
                second_url: self._article_html("乙", body_text),
            }
        )

        store = Store(self.db_path, self.data_dir)
        articles = {
            row["url"]
            for row in store_core(store).execute("SELECT url FROM articles")
        }
        second_page = store_core(store).execute(
            "SELECT duplicate_of FROM pages WHERE url=?", (second_url,)
        ).fetchone()
        store.close()

        self.assertEqual(articles, {first_url})
        self.assertEqual(second_page["duplicate_of"], first_url)

    async def test_different_body_articles_are_both_indexed(self) -> None:
        first_url = "https://dup.sample.cn/info/1055/1001.htm"
        second_url = "https://dup.sample.cn/info/1055/1002.htm"
        await self._process_column_pages(
            {
                first_url: self._article_html("甲", "第一篇文章的正文内容，足够长。" * 20),
                second_url: self._article_html("乙", "第二篇文章写着完全不同的内容。" * 20),
            }
        )

        store = Store(self.db_path, self.data_dir)
        articles = {
            row["url"]
            for row in store_core(store).execute("SELECT url FROM articles")
        }
        second_page = store_core(store).execute(
            "SELECT duplicate_of FROM pages WHERE url=?", (second_url,)
        ).fetchone()
        store.close()

        self.assertEqual(articles, {first_url, second_url})
        self.assertEqual(second_page["duplicate_of"], "")

    async def test_short_identical_bodies_are_not_deduplicated(self) -> None:
        # Brief notices share boilerplate-heavy bodies; below the content
        # dedup floor they keep their own article rows.
        first_url = "https://dup.sample.cn/info/1055/1001.htm"
        second_url = "https://dup.sample.cn/info/1055/1002.htm"
        body_text = "简短通知正文，不足内容判重下限。"
        await self._process_column_pages(
            {
                first_url: self._article_html("甲", body_text),
                second_url: self._article_html("乙", body_text),
            }
        )

        store = Store(self.db_path, self.data_dir)
        articles = {
            row["url"]
            for row in store_core(store).execute("SELECT url FROM articles")
        }
        second_page = store_core(store).execute(
            "SELECT duplicate_of FROM pages WHERE url=?", (second_url,)
        ).fetchone()
        store.close()

        self.assertEqual(articles, {first_url, second_url})
        self.assertEqual(second_page["duplicate_of"], "")


    async def test_process_resolves_duplicate_digest_once_per_page(self) -> None:
        # The body must exceed the small-page duplicate floor so the digest
        # lookup runs at all.
        padding = "查重填充段落，用来让页面体超过小页面判重下限。" * 100
        html = f"""<html><body><article>
          <h1>查重计数新闻</h1>
          <p>这是足够长的正文内容，用来验证每页只进行一次内容查重查询。</p>
          <p>{padding}</p>
        </article></body></html>"""
        crawler = self._crawler()

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        calls = 0
        original = crawler.store.duplicate_page_url

        def counting(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        crawler.store.duplicate_page_url = counting
        await crawler._process("https://news.example.test/article/dup-count", "news", 0, "")
        await crawler.close()

        self.assertEqual(calls, 1)

    async def test_new_article_bundle_is_written_once(self) -> None:
        html = """<html><body><article>
          <h1>单次落盘新闻</h1>
          <p>这是足够长的正文内容，用来验证新文章的本地归档 bundle 只写入一次。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
        </article></body></html>"""
        crawler = self._crawler()

        async def fake_fetch(url: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        writes = 0
        original = crawler.store.write_article_bundle

        def counting(article, content_hash=None):
            nonlocal writes
            writes += 1
            return original(article, content_hash)

        crawler.store.write_article_bundle = counting
        await crawler._process("https://news.example.test/article/once", "news", 0, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        article = store_core(store).execute(
            "SELECT url FROM articles WHERE url=?",
            ("https://news.example.test/article/once",),
        ).fetchone()
        store.close()

        self.assertIsNotNone(article)
        self.assertEqual(writes, 1)

    async def test_normal_crawl_does_not_download_or_link_article_images(self) -> None:
        url = "https://news.example.test/article/shared-image"
        image_url = "https://news.example.test/img/shared.jpg"
        store = Store(self.db_path, self.data_dir)
        store.save_article(
            ArticleDocument(
                url="https://news.example.test/article/original",
                source_id="news",
                title="原始文章",
                author="",
                published_at="",
                updated_at="",
                category="",
                summary="",
                body_html="<p>正文</p>",
                body_text="正文",
                body_markdown="正文",
                extraction_method="test",
                source_page_url="https://news.example.test/article/original",
            )
        )
        store.save_media(
            ImageRef(url=image_url, alt="", title="", caption=""),
            b"shared-image-bytes",
            "image/jpeg",
            "https://news.example.test/article/original",
            "",
        )
        store.close()

        html = f"""<html><body><article>
          <h1>共享图片新闻</h1>
          <p>这是足够长的正文内容，用来验证已经下载过的共享图片不会被重复请求。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
          <img src="{image_url}" />
        </article></body></html>"""
        crawler = self._crawler()
        fetched: list[str] = []

        async def fake_fetch(requested: str, *, max_bytes: int | None = None) -> FetchResponse:
            fetched.append(requested)
            return FetchResponse(
                requested_url=requested,
                final_url=requested,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process(url, "news", 1, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        link = store_core(store).execute(
            "SELECT local_path FROM article_media WHERE article_url=? AND image_url=?",
            (url, image_url),
        ).fetchone()
        store.close()

        self.assertNotIn(image_url, fetched)
        self.assertIsNone(link)

    async def test_normal_crawl_does_not_consult_legacy_media_records(self) -> None:
        # Existing archival media remains available to the explicit
        # ``download-images`` command; the normal crawl only records source
        # URLs and never turns those rows into article sync objects.
        url = "https://news.example.test/article/legacy-image"
        image_url = "https://news.example.test/img/legacy.jpg"
        store = Store(self.db_path, self.data_dir)
        store.save_article(
            ArticleDocument(
                url="https://news.example.test/article/original",
                source_id="news",
                title="原始文章",
                author="",
                published_at="",
                updated_at="",
                category="",
                summary="",
                body_html="<p>正文</p>",
                body_text="正文",
                body_markdown="正文",
                extraction_method="test",
                source_page_url="https://news.example.test/article/original",
            )
        )
        store.save_media(
            ImageRef(url=image_url, alt="", title="", caption=""),
            b"legacy-image-bytes",
            "image/jpeg",
            "https://news.example.test/article/original",
            "",
        )
        store_core(store).execute("UPDATE media SET size=0 WHERE url=?", (image_url,))
        store_core(store).commit()
        store.close()

        html = f"""<html><body><article>
          <h1>旧记录图片新闻</h1>
          <p>这是足够长的正文内容，用来验证 size 未知的旧 ok 记录不会触发重复下载。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
          <img src="{image_url}" />
        </article></body></html>"""
        crawler = self._crawler()
        fetched: list[str] = []

        async def fake_fetch(requested: str, *, max_bytes: int | None = None) -> FetchResponse:
            fetched.append(requested)
            return FetchResponse(
                requested_url=requested,
                final_url=requested,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process(url, "news", 1, "")
        await crawler.close()

        self.assertNotIn(image_url, fetched)
        store = Store(self.db_path, self.data_dir)
        link = store_core(store).execute(
            "SELECT 1 FROM article_media WHERE article_url=? AND image_url=?",
            (url, image_url),
        ).fetchone()
        store.close()
        self.assertIsNone(link)

    async def test_incremental_article_page_still_enqueues_document_attachments(self) -> None:
        url = "https://news.example.test/article/with-attachment"
        attachment = "https://news.example.test/files/notice.pdf"
        archive_link = "https://news.example.test/archive/old.htm"
        html = f"""<html><body><article>
          <h1>带附件的新文章</h1>
          <p>这是足够长的正文内容，用来验证增量模式下新文章的附件出链仍然会被抓取。</p>
          <p>第二段正文确保页面得分可以达到索引阈值。</p>
          <a href="{attachment}">附件</a>
          <a href="{archive_link}">历史归档</a>
        </article></body></html>"""
        crawler = self._crawler()
        crawler.options.incremental = True
        crawler.source_since = {"news": _parse_since("2020-01-01")}

        async def fake_fetch(requested: str, *, max_bytes: int | None = None) -> FetchResponse:
            return FetchResponse(
                requested_url=requested,
                final_url=requested,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=html.encode("utf-8"),
            )

        crawler.fetcher.fetch = fake_fetch
        await crawler._process(url, "news", 1, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        rows = {
            row["url"]: row["status"]
            for row in store_core(store).execute(
                "SELECT url, status FROM frontier WHERE url IN (?, ?)",
                (attachment, archive_link),
            ).fetchall()
        }
        store.close()

        self.assertEqual(rows.get(attachment), "pending")
        self.assertIsNone(rows.get(archive_link))


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.db_path = self.data_dir / "crawler.sqlite"
        store = Store(self.db_path, self.data_dir)
        store.add_source(
            SourceConfig(
                id="news",
                name="测试新闻",
                organization_level="university",
                seed_urls=["https://news.example.test/"],
                allowed_hosts=["news.example.test"],
            )
        )
        store.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _crawler(self, concurrency: int = 4) -> AsyncCrawler:
        crawler = AsyncCrawler(
            CrawlOptions(
                db_path=str(self.db_path),
                data_dir=str(self.data_dir),
                concurrency=concurrency,
            )
        )
        crawler.prepare = lambda: None  # keep the worker lifecycle tests offline
        crawler.store.start_sync_run(crawler.sync_run_id, mode="full")
        crawler.sync_run_started = True

        async def _no_seed() -> None:
            return None

        crawler._seed_queue = _no_seed
        return crawler

    async def test_workers_process_queue_concurrently(self) -> None:
        crawler = self._crawler(concurrency=4)
        in_flight = 0
        peak = 0
        done: list[str] = []

        async def fake_process(url: str, source_id: str, depth: int, parent: str) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            done.append(url)
            in_flight -= 1

        crawler._process = fake_process
        for index in range(8):
            await crawler.queue.put((0, index + 1, f"https://news.example.test/{index}", "news", 0, ""))

        await crawler.run()
        await crawler.close()

        self.assertEqual(len(done), 8)
        self.assertGreaterEqual(peak, 2)

    async def test_workers_survive_idle_gap_for_late_enqueued_items(self) -> None:
        crawler = self._crawler(concurrency=4)
        in_flight = 0
        peak = 0

        async def fake_process(url: str, source_id: str, depth: int, parent: str) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            if url.endswith("/first"):
                # A slow page that discovers new links after the old one
                # second idle timeout: sibling workers must still be alive
                # to pick the late items up concurrently.
                await asyncio.sleep(1.3)
                for index in range(3):
                    await crawler.queue.put(
                        (0, 10 + index, f"https://news.example.test/late{index}", "news", 0, "")
                    )
            else:
                await asyncio.sleep(0.2)
            in_flight -= 1

        crawler._process = fake_process
        await crawler.queue.put((0, 1, "https://news.example.test/first", "news", 0, ""))

        await crawler.run()
        await crawler.close()

        self.assertGreaterEqual(peak, 2)

    async def test_cancel_marks_sync_run_interrupted(self) -> None:
        crawler = self._crawler(concurrency=1)

        async def blocking_process(url: str, source_id: str, depth: int, parent: str) -> None:
            await asyncio.sleep(30)

        crawler._process = blocking_process
        await crawler.queue.put((0, 1, "https://news.example.test/stuck", "news", 0, ""))

        task = asyncio.create_task(crawler.run())
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
            "SELECT status FROM sync_runs WHERE id=?", (crawler.sync_run_id,)
        ).fetchone()
        store.close()
        await crawler.close()

        self.assertEqual(row["status"], "interrupted")


class SeedRevivalTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_revives_errored_seed_below_attempt_cap(self) -> None:
        # nercslip's seed once answered 404 and stayed as an error row, so the
        # whole source went dark (that source has since been removed; mcip
        # stands in as the supplemental fixture).  Every run must retry an
        # errored seed until the attempt cap, but no further.
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        retry_seed = "https://mcip.ustc.edu.cn/main.htm"
        capped_seed = "https://yz1.ustc.edu.cn/"
        store = Store(root / "crawler.sqlite", root / "data")
        for source_id, seed, host in (
            ("supplemental-mcip", retry_seed, "mcip.ustc.edu.cn"),
            ("supplemental-yz1", capped_seed, "yz1.ustc.edu.cn"),
        ):
            store.add_source(
                SourceConfig(
                    id=source_id,
                    name=source_id,
                    organization_level="research",
                    seed_urls=[seed],
                    allowed_hosts=[host],
                    discovery_only=True,
                )
            )
        store.enqueue(retry_seed, "supplemental-mcip", 0, "", 500)
        store.enqueue(capped_seed, "supplemental-yz1", 0, "", 500)
        store_core(store).execute(
            "UPDATE frontier SET status='error', attempts=1, last_error='http 404' WHERE url=?",
            (retry_seed,),
        )
        store_core(store).execute(
            "UPDATE frontier SET status='error', attempts=5, last_error='http 404' WHERE url=?",
            (capped_seed,),
        )
        store_core(store).commit()
        store.close()

        crawler = AsyncCrawler(
            CrawlOptions(
                db_path=str(root / "crawler.sqlite"),
                data_dir=str(root / "data"),
                include_supplemental=True,
            )
        )
        crawler.prepare()
        statuses = {
            row["url"]: row["status"]
            for row in store_core(crawler.store).execute(
                "SELECT url, status FROM frontier WHERE url IN (?, ?)",
                (retry_seed, capped_seed),
            ).fetchall()
        }
        await crawler.close()

        self.assertEqual(statuses[retry_seed], "pending")
        self.assertEqual(statuses[capped_seed], "error")


if __name__ == "__main__":
    unittest.main()
