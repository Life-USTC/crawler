import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ustc_crawler.crawl import AsyncCrawler, CrawlOptions, _parse_since
from ustc_crawler.models import PageDocument, SourceConfig
from ustc_crawler.routing import source_id_for_url
from ustc_crawler.store import Store


def source(source_id: str, host: str) -> SourceConfig:
    return SourceConfig(
        id=source_id,
        name=source_id,
        organization_level="test",
        seed_urls=[f"https://{host}/"],
        allowed_hosts=[host],
    )


class SourceRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.db_path = self.data_dir / "crawler.sqlite"
        self.university = source("university", "www.example.test")
        self.news = source("news", "news.example.test")
        store = Store(self.db_path, self.data_dir)
        store.add_sources([self.university, self.news])
        store.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def crawler(self) -> AsyncCrawler:
        crawler = AsyncCrawler(
            CrawlOptions(db_path=str(self.db_path), data_dir=str(self.data_dir))
        )
        crawler.configured_sources = {
            "university": self.university,
            "news": self.news,
        }
        crawler.sources = dict(crawler.configured_sources)
        return crawler

    async def test_cross_host_link_is_enqueued_under_owning_source(self) -> None:
        crawler = self.crawler()
        url = "https://news.example.test/info/1/2.htm"

        await crawler._enqueue(url, self.university, 1, "https://www.example.test/")
        item = await crawler.queue.get()
        crawler.queue.task_done()
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store.db.execute(
            "SELECT source_id,status FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(row["source_id"], "news")
        self.assertEqual(item[3], "news")
        store.close()

    async def test_inactive_owner_handoff_stays_pending_without_active_queue_item(self) -> None:
        crawler = self.crawler()
        crawler.sources = {"university": self.university}
        url = "https://news.example.test/info/1/3.htm"

        await crawler._enqueue(url, self.university, 1, "https://www.example.test/")
        self.assertTrue(crawler.queue.empty())
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store.db.execute(
            "SELECT source_id,status FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(dict(row), {"source_id": "news", "status": "pending"})
        store.close()

    async def test_shallow_incremental_gap_revives_filtered_url(self) -> None:
        url = "https://news.example.test/2026/0824/c1a2/page.htm"
        store = Store(self.db_path, self.data_dir)
        store.enqueue(url, "news", 1, "https://news.example.test/", 100)
        store.mark_filtered(url, "published 2026-08-24 is before incremental cutoff")
        store.close()
        crawler = self.crawler()
        crawler.options.incremental = True
        crawler.source_since = {"news": _parse_since("2026-08-30")}

        await crawler._enqueue(url, self.news, 1, "https://news.example.test/")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store.db.execute(
            "SELECT status,last_error FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertFalse(row["last_error"])
        store.close()

    async def test_shallow_incremental_gap_revives_done_public_page_without_article(self) -> None:
        url = "https://news.example.test/2026/0824/c1a3/page.htm"
        store = Store(self.db_path, self.data_dir)
        store.enqueue(url, "news", 1, "https://news.example.test/", 100)
        store.save_page(
            PageDocument(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                fetched_at="2026-08-24T00:00:00+08:00",
                title="current article missing extraction",
                canonical_url=url,
                html="<html><body>article</body></html>",
                links=[],
                images=[],
                page_kind="news_article",
                access_mode="public",
            ),
            "news",
            1,
        )
        store.mark_done(url)
        store.close()
        crawler = self.crawler()
        crawler.options.incremental = True
        crawler.source_since = {"news": _parse_since("2026-08-30")}

        await crawler._enqueue(url, self.news, 1, "https://news.example.test/")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store.db.execute(
            "SELECT status FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        store.close()

    async def test_unloaded_optional_source_pending_row_is_not_filtered(self) -> None:
        url = "https://unit.example.test/article.htm"
        store = Store(self.db_path, self.data_dir)
        store.add_source(source("unit", "unit.example.test"))
        store.enqueue(url, "unit", 1, "https://unit.example.test/")
        store.close()
        crawler = self.crawler()

        await crawler._seed_queue()
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store.db.execute(
            "SELECT status FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        store.close()

    def test_longest_host_match_has_deterministic_ownership(self) -> None:
        owner = source_id_for_url(
            "https://news.example.test/article.htm",
            {
                "broad": (["example.test"], []),
                "z-news": (["news.example.test"], []),
                "a-news": (["news.example.test"], []),
            },
        )
        self.assertEqual(owner, "a-news")


if __name__ == "__main__":
    unittest.main()
