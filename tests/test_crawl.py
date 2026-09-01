import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from ustc_crawler.crawl import (
    AsyncCrawler,
    CrawlOptions,
    _is_after_since,
    _parse_since,
)
from ustc_crawler.models import ArticleDocument, FetchResponse, PageDocument, SourceConfig
from ustc_crawler.store import Store


class SinceHelpersTests(unittest.TestCase):
    def test_parse_since_rejects_invalid_date(self) -> None:
        self.assertIsNone(_parse_since("not-a-date"))
        self.assertIsNone(_parse_since(""))

    def test_is_after_since_handles_missing_or_invalid_dates(self) -> None:
        since = _parse_since("2025-01-01")
        self.assertTrue(_is_after_since("", since))
        self.assertTrue(_is_after_since("invalid", since))


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
                            if url == urls["missing_sitemap"][0]
                            else "news_listing"
                        ),
                    ),
                    "news",
                    depth,
                )
                store.mark_done(url)
            store.mark_done(urls["missing_sitemap"][0], "http 404")

            # Simulate an interrupted older incremental run.
            store.db.execute(
                "UPDATE frontier SET status='pending' WHERE url=?",
                (urls["deep"][0],),
            )
            store.db.commit()

            store.reset_seeds_and_listings({"news"})
            statuses = {
                row["url"]: row["status"]
                for row in store.db.execute("SELECT url,status FROM frontier")
            }
            store.close()

        self.assertEqual(statuses[urls["seed"][0]], "pending")
        self.assertEqual(statuses[urls["shallow"][0]], "pending")
        self.assertEqual(statuses[urls["deep"][0]], "done")
        self.assertEqual(statuses[urls["missing_sitemap"][0]], "error")


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
            for row in store.db.execute("SELECT url, status FROM frontier").fetchall()
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
        row = store.db.execute(
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
        store.db.execute("UPDATE frontier SET status='pending' WHERE url=?", (url,))
        store.db.commit()
        store.close()

        crawler = self._crawler()
        crawler.options.incremental = True
        await crawler._seed_queue()
        self.assertTrue(crawler.queue.empty())
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store.db.execute(
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
        row = store.db.execute(
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
        row = store.db.execute(
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
        row = store.db.execute(
            "SELECT 1 FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertIsNone(row)
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
        row = store.db.execute(
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
        article = store.db.execute(
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
        article = store.db.execute(
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
        article = store.db.execute(
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
        article = store.db.execute(
            "SELECT title FROM articles WHERE url=?", (url,)
        ).fetchone()
        related = store.db.execute(
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
        article = store.db.execute(
            "SELECT published_at FROM articles WHERE url=?", (final_url,)
        ).fetchone()
        page = store.db.execute(
            "SELECT published_at FROM pages WHERE url=?", (requested_url,)
        ).fetchone()
        self.assertIsNotNone(article)
        self.assertEqual(article["published_at"], "2026-08-30")
        self.assertEqual(page["published_at"], "2026-08-30")
        store.close()


if __name__ == "__main__":
    unittest.main()
