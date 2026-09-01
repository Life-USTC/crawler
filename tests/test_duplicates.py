import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ustc_crawler.models import ArticleDocument, PageDocument, SourceConfig
from ustc_crawler.store import Store, article_bundle_path


class DuplicateKeeperTests(unittest.TestCase):
    def test_equal_bodies_have_distinct_url_specific_bundles(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            store.add_source(
                SourceConfig(
                    id="source",
                    name="source",
                    organization_level="test",
                    seed_urls=[],
                    allowed_hosts=["example.test"],
                )
            )
            hashes = []
            urls = ["https://example.test/a", "https://example.test/b"]
            for url in urls:
                hashes.append(
                    store.save_article(
                        ArticleDocument(
                            url=url,
                            source_id="source",
                            title=url.rsplit("/", 1)[-1],
                            author="",
                            published_at="",
                            updated_at="",
                            category="",
                            summary="",
                            body_html="<p>same body</p>",
                            body_text="same body",
                            body_markdown="same body",
                            extraction_method="test",
                            source_page_url=url,
                        )
                    )
                )
            store.close()

            self.assertEqual(hashes[0], hashes[1])
            bundles = [article_bundle_path(root / "data", url) for url in urls]
            self.assertNotEqual(bundles[0], bundles[1])
            self.assertEqual(json.loads(bundles[0].read_text())["url"], urls[0])
            self.assertEqual(json.loads(bundles[1].read_text())["url"], urls[1])
            self.assertEqual(json.loads(bundles[0].read_text())["content_hash"], hashes[0])

    def test_indexed_article_becomes_stable_keeper(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            store.add_source(
                SourceConfig(
                    id="source",
                    name="source",
                    organization_level="test",
                    seed_urls=[],
                    allowed_hosts=["example.test"],
                )
            )
            nonarticle_url = "https://example.test/nonarticle"
            article_url = "https://example.test/article"
            for url in (nonarticle_url, article_url):
                store.save_page(
                    PageDocument(
                        requested_url=url,
                        final_url=url,
                        status=200,
                        content_type="text/html",
                        fetched_at="2026-01-01T00:00:00+08:00",
                        title="same",
                        canonical_url=url,
                        html="<html><body>same</body></html>",
                        links=[],
                        images=[],
                    ),
                    "source",
                    0,
                )
            store.save_article(
                ArticleDocument(
                    url=article_url,
                    source_id="source",
                    title="article",
                    author="",
                    published_at="",
                    updated_at="",
                    category="",
                    summary="",
                    body_html="<p>body</p>",
                    body_text="body",
                    body_markdown="body",
                    extraction_method="test",
                    source_page_url=article_url,
                )
            )

            store.rescore_pages()
            rows = {
                row["url"]: row["duplicate_of"]
                for row in store.db.execute("SELECT url,duplicate_of FROM pages")
            }
            digest = store.db.execute(
                "SELECT sha256 FROM pages WHERE url=?", (article_url,)
            ).fetchone()[0]
            keeper = store.duplicate_page_url(
                digest,
                article_url,
                prefer_article=True,
            )
            store.close()

        self.assertFalse(rows[article_url])
        self.assertEqual(rows[nonarticle_url], article_url)
        self.assertFalse(keeper)


if __name__ == "__main__":
    unittest.main()
