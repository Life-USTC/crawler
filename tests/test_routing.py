import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support import store_core
from ustc_crawler.crawl import AsyncCrawler, CrawlOptions, _parse_since
from ustc_crawler.models import FetchResponse, PageDocument, SourceConfig
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
        row = store_core(store).execute(
            "SELECT source_id,status FROM frontier WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(row["source_id"], "news")
        self.assertEqual(item[3], "news")
        store.close()

    async def test_canonical_article_uses_owner_but_page_keeps_fetched_source(self) -> None:
        fetched_url = "https://www.example.test/info/1/2.htm"
        canonical_url = "https://news.example.test/info/1/2.htm"
        html = f"""<html><head>
          <meta property="og:url" content="{canonical_url}">
        </head><body><article>
          <h1>Canonical news article</h1>
          <p>This body is long enough for the page to be retained as a public article.</p>
          <p>The fetched page remains attributed to the university source.</p>
        </article></body></html>"""
        crawler = self.crawler()

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
        await crawler._process(fetched_url, "university", 0, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        page = store_core(store).execute(
            "SELECT source_id,canonical_url FROM pages WHERE url=?", (fetched_url,)
        ).fetchone()
        article = store_core(store).execute(
            "SELECT source_id,source_page_url FROM articles WHERE url=?", (canonical_url,)
        ).fetchone()
        outbox = store_core(store).execute(
            "SELECT payload_json FROM sync_outbox WHERE entity_key=?",
            (f"news:{canonical_url}",),
        ).fetchone()
        self.assertEqual(dict(page), {"source_id": "university", "canonical_url": canonical_url})
        self.assertEqual(
            dict(article), {"source_id": "news", "source_page_url": fetched_url}
        )
        self.assertEqual(json.loads(outbox["payload_json"])["sourceId"], "news")
        store.close()

    async def test_unowned_canonical_article_is_archived_without_publication(self) -> None:
        fetched_url = "https://www.example.test/info/1/3.htm"
        canonical_url = "https://external.example.net/?p=4663"
        html = f"""<html><head>
          <meta property="og:url" content="{canonical_url}">
        </head><body><article>
          <h1>Escaped canonical article</h1>
          <p>This body is long enough to look like a public article before ownership is checked.</p>
          <p>The raw page should remain available for audit.</p>
        </article></body></html>"""
        crawler = self.crawler()

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
        await crawler._process(fetched_url, "university", 0, "")
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        page = store_core(store).execute(
            "SELECT source_id,canonical_url,raw_path FROM pages WHERE url=?", (fetched_url,)
        ).fetchone()
        article = store_core(store).execute(
            "SELECT 1 FROM articles WHERE url=?", (canonical_url,)
        ).fetchone()
        outbox = store_core(store).execute(
            "SELECT 1 FROM sync_outbox WHERE entity_key=?",
            (f"university:{canonical_url}",),
        ).fetchone()
        self.assertEqual(page["source_id"], "university")
        self.assertEqual(page["canonical_url"], canonical_url)
        self.assertTrue(Path(page["raw_path"]).is_file())
        self.assertIsNone(article)
        self.assertIsNone(outbox)
        store.close()

    async def test_inactive_owner_handoff_stays_pending_without_active_queue_item(self) -> None:
        crawler = self.crawler()
        crawler.sources = {"university": self.university}
        url = "https://news.example.test/info/1/3.htm"

        await crawler._enqueue(url, self.university, 1, "https://www.example.test/")
        self.assertTrue(crawler.queue.empty())
        await crawler.close()

        store = Store(self.db_path, self.data_dir)
        row = store_core(store).execute(
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
        row = store_core(store).execute(
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
        row = store_core(store).execute(
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
        row = store_core(store).execute(
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
