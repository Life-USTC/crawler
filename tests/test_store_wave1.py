import os
import unittest
from datetime import UTC
from pathlib import Path
from tempfile import TemporaryDirectory

from ustc_crawler.models import ArticleDocument, ImageRef, PageDocument, SourceConfig
from ustc_crawler.store import Store, _atomic_write_bytes


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_bytes_creates_target_without_temp_residue(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "sub" / "file.bin"
            _atomic_write_bytes(target, b"payload")
            self.assertEqual(target.read_bytes(), b"payload")
            leftovers = [p for p in Path(temp).rglob("*") if p.is_file()]
            self.assertEqual(leftovers, [target])

    def test_atomic_write_bytes_replaces_existing_target(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "file.bin"
            target.write_bytes(b"old")
            _atomic_write_bytes(target, b"new")
            self.assertEqual(target.read_bytes(), b"new")

    def test_atomic_write_bytes_cleans_up_temp_file_on_failure(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "file.bin"

            original_replace = os.replace

            def failing_replace(src, dst):
                raise OSError("simulated crash")

            import ustc_crawler.store as store_module

            store_module.os.replace = failing_replace
            try:
                with self.assertRaises(OSError):
                    _atomic_write_bytes(target, b"payload")
            finally:
                store_module.os.replace = original_replace
            self.assertEqual([p for p in Path(temp).iterdir()], [])


class MediaIntegrityTests(unittest.TestCase):
    @staticmethod
    def _save_article(store: Store, url: str) -> None:
        store.add_source(
            SourceConfig(
                id="news",
                name="测试新闻",
                organization_level="university",
                seed_urls=["https://example.test/"],
                allowed_hosts=["example.test"],
            )
        )
        store.save_article(
            ArticleDocument(
                url=url,
                source_id="news",
                title="标题",
                author="",
                published_at="",
                updated_at="",
                category="",
                summary="",
                body_html="<p>正文</p>",
                body_text="正文",
                body_markdown="正文",
                extraction_method="test",
                source_page_url=url,
            )
        )

    def test_save_media_error_does_not_clobber_existing_ok_record(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            self._save_article(store, "https://example.test/a")
            image = ImageRef(url="https://example.test/a.jpg", alt="a", title="", caption="")
            local = store.save_media(image, b"image-bytes", "image/jpeg", "https://example.test/a", "")
            self.assertIsNotNone(local)

            store.save_media(
                image, b"", "image/jpeg", "https://example.test/a", "", error="http 503"
            )
            snapshot = store.media_snapshot(image.url)
            store.close()

            self.assertEqual(snapshot["status"], "ok")
            self.assertEqual(snapshot["local_path"], str(local))
            self.assertTrue(Path(snapshot["local_path"]).is_file())

    def test_media_snapshot_reports_size_for_integrity_checks(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            self._save_article(store, "https://example.test/b")
            image = ImageRef(url="https://example.test/b.jpg", alt="", title="", caption="")
            store.save_media(image, b"12345", "image/jpeg", "https://example.test/b", "")
            snapshot = store.media_snapshot(image.url)
            store.close()

            self.assertEqual(snapshot["size"], 5)


class ArticleBundleAtomicTests(unittest.TestCase):
    def test_write_article_bundle_leaves_no_partial_files(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            article = ArticleDocument(
                url="https://example.test/article/1",
                source_id="news",
                title="标题",
                author="",
                published_at="",
                updated_at="",
                category="",
                summary="",
                body_html="<p>正文</p>",
                body_text="正文",
                body_markdown="正文",
                extraction_method="test",
                source_page_url="https://example.test/article/1",
            )
            store.write_article_bundle(article)
            articles_dir = root / "data" / "articles"
            files = sorted(p.name for p in articles_dir.iterdir())
            store.close()

            self.assertEqual(len(files), 2)
            self.assertTrue(all(not name.startswith(".") for name in files))


class FrontierRevivalTests(unittest.TestCase):
    def test_errored_frontier_row_is_revived_below_attempt_cap(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            url = "https://example.test/flaky"
            store.add_source(
                SourceConfig(
                    id="news",
                    name="测试新闻",
                    organization_level="university",
                    seed_urls=["https://example.test/"],
                    allowed_hosts=["example.test"],
                )
            )
            store.enqueue(url, "news", 1, "", 100)
            store.mark_processing(url)
            store.mark_done(url, "http 503")

            revived = store.enqueue(url, "news", 1, "", 100)
            from tests.support import store_core

            row = store_core(store).execute(
                "SELECT status, attempts FROM frontier WHERE url=?", (url,)
            ).fetchone()
            store.close()

            self.assertTrue(revived)
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 1)

    def test_errored_frontier_row_stays_terminal_at_attempt_cap(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            url = "https://example.test/dead"
            store.add_source(
                SourceConfig(
                    id="news",
                    name="测试新闻",
                    organization_level="university",
                    seed_urls=["https://example.test/"],
                    allowed_hosts=["example.test"],
                )
            )
            store.enqueue(url, "news", 1, "", 100)
            for _ in range(5):
                store.mark_processing(url)
                store.mark_done(url, "http 503")

            revived = store.enqueue(url, "news", 1, "", 100)
            from tests.support import store_core

            row = store_core(store).execute(
                "SELECT status, attempts FROM frontier WHERE url=?", (url,)
            ).fetchone()
            store.close()

            self.assertFalse(revived)
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["attempts"], 5)


def _make_article(url: str, published_at: str) -> ArticleDocument:
    return ArticleDocument(
        url=url,
        source_id="news",
        title="标题",
        author="",
        published_at=published_at,
        updated_at="",
        category="",
        summary="",
        body_html="<p>正文</p>",
        body_text="正文",
        body_markdown="正文",
        extraction_method="test",
        source_page_url=url,
    )


class StoreWave1Tests(unittest.TestCase):
    def _store(self, root: Path) -> Store:
        store = Store(root / "crawler.sqlite", root / "data")
        store.add_source(
            SourceConfig(
                id="news",
                name="测试新闻",
                organization_level="university",
                seed_urls=["https://example.test/"],
                allowed_hosts=["example.test"],
            )
        )
        return store

    def test_save_page_keeps_existing_published_at_when_new_value_empty(self) -> None:
        with TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            url = "https://example.test/page"
            store.save_page(
                PageDocument(
                    requested_url=url, final_url=url, status=200, content_type="text/html",
                    fetched_at="2026-01-01T00:00:00+08:00", title="t", canonical_url=url,
                    html="<html></html>", links=[], images=[], published_at="2026-01-01",
                ),
                "news", 0,
            )
            store.save_page(
                PageDocument(
                    requested_url=url, final_url=url, status=404, content_type="",
                    fetched_at="2026-02-01T00:00:00+08:00", title="", canonical_url=url,
                    html="", links=[], images=[], error="http 404",
                ),
                "news", 0,
            )
            from tests.support import store_core

            row = store_core(store).execute(
                "SELECT published_at FROM pages WHERE url=?", (url,)
            ).fetchone()
            store.close()
            self.assertEqual(row["published_at"], "2026-01-01")

    def test_failure_upserts_by_url_and_counts_attempts(self) -> None:
        with TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            store.failure("https://example.test/x", "news", "http 503", 503)
            store.failure("https://example.test/x", "news", "http 500", 500)
            store.failure("https://example.test/y", "news", "http 404", 404)
            from tests.support import store_core

            rows = {
                row["url"]: row
                for row in store_core(store).execute("SELECT * FROM failures").fetchall()
            }
            store.close()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows["https://example.test/x"]["attempts"], 2)
            self.assertEqual(rows["https://example.test/x"]["error"], "http 500")

    def test_source_newest_dates_compares_aware_datetimes_across_offsets(self) -> None:
        with TemporaryDirectory() as temp:
            store = self._store(Path(temp))
            # Same wall-clock ordering trap: the +08:00 string sorts after the
            # UTC string as text, but the UTC instant is actually newer.
            store.save_article(_make_article("https://example.test/a", "2026-08-01T12:00:00+08:00"))
            store.save_article(_make_article("https://example.test/b", "2026-08-01T06:00:00+00:00"))
            dates = store.source_newest_dates({"news"})
            store.close()
            from datetime import datetime

            self.assertEqual(dates["news"], datetime(2026, 8, 1, 6, 0, tzinfo=UTC))

    def test_export_jsonl_includes_images_without_per_row_queries(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            article = _make_article("https://example.test/a", "2026-01-01")
            store.save_article(article)
            store.save_media(
                ImageRef(url="https://example.test/i.jpg", alt="a", title="", caption=""),
                b"img",
                "image/jpeg",
                article.url,
                "",
            )
            target = store.export_jsonl(root / "out" / "articles.jsonl")
            store.close()
            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            import json as json_module

            item = json_module.loads(lines[0])
            self.assertEqual(item["images"][0]["url"], "https://example.test/i.jpg")

    def test_retext_recomputes_only_changed_bodies(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            article = _make_article("https://example.test/a", "2026-01-01")
            article.body_html = "<p>重新计算的正文</p>"
            article.body_text = "过时的正文"
            store.save_article(article)
            result = store.retext_article_bodies()
            from tests.support import store_core

            row = store_core(store).execute(
                "SELECT body_text FROM articles WHERE url=?", (article.url,)
            ).fetchone()
            store.close()
            self.assertEqual(result, {"scanned": 1, "changed": 1})
            self.assertIn("重新计算的正文", row["body_text"])


if __name__ == "__main__":
    unittest.main()
