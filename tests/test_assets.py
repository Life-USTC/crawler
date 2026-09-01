import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support import store_core
from ustc_crawler.db.models import Media
from ustc_crawler.models import ArticleDocument, ImageRef, SourceConfig
from ustc_crawler.store import Store


class AssetStorageTests(unittest.TestCase):
    def test_empty_asset_body_is_an_error(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            url = "https://example.ustc.edu.cn/empty.docx"

            path = store.save_asset(
                url=url,
                source_url="https://example.ustc.edu.cn/article.htm",
                body=b"",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            row = store_core(store).execute(
                "SELECT status,error,size,local_path FROM assets WHERE url=?", (url,)
            ).fetchone()
            store.close()

        self.assertIsNone(path)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], "empty response body")
        self.assertEqual(row["size"], 0)
        self.assertFalse(row["local_path"])

    def test_shared_media_is_selected_from_article_media_link(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            store.add_source(
                SourceConfig(
                    id="news",
                    name="新闻网",
                    organization_level="university",
                    seed_urls=["https://news.example.test/"],
                    allowed_hosts=["news.example.test"],
                )
            )
            article_url = "https://news.example.test/article/1"
            store.save_article(
                ArticleDocument(
                    url=article_url,
                    source_id="news",
                    title="标题",
                    author="",
                    published_at="2026-08-20",
                    updated_at="",
                    category="",
                    summary="",
                    body_html="<p>正文</p>",
                    body_text="正文",
                    body_markdown="正文",
                    extraction_method="test",
                    source_page_url=article_url,
                )
            )
            image = ImageRef(
                url="https://news.example.test/upload/shared.png",
                article_url=article_url,
            )
            path = store.save_media(
                image,
                b"shared image",
                "image/png",
                article_url,
                article_url,
            )
            assert path is not None
            # A reusable media object has no single owning article.  Its
            # article_media row remains the authoritative article link.
            with store.database.session_factory.begin() as session:
                session.get(Media, image.url).article_url = None

            self.assertEqual(
                store.media_paths_for_article(article_url),
                {image.url: (str(path), "image/png")},
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
