import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ustc_crawler.cli import main as cli_main
from ustc_crawler.extract import extract_page
from ustc_crawler.models import ArticleDocument, ImageRef, SourceConfig
from ustc_crawler.store import Store


class CleanupBlockedHostArticlesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.db_path = self.data_dir / "crawler.sqlite"
        self.store = Store(self.db_path, self.data_dir)
        self.store.add_source(
            SourceConfig(
                id="library",
                name="图书馆",
                organization_level="service",
                seed_urls=["https://lib.ustc.edu.cn/"],
                allowed_hosts=["lib.ustc.edu.cn"],
                blocked_hosts=["career.lib.ustc.edu.cn", "mirror.lib.ustc.edu.cn"],
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _save_article(self, url: str, source_id: str = "library") -> None:
        article = ArticleDocument(
            url=url,
            source_id=source_id,
            title="标题",
            author="",
            published_at="2026-08-01",
            updated_at="",
            category="",
            summary="摘要",
            body_html="<p>正文</p>",
            body_text="正文",
            body_markdown="正文",
            extraction_method="test",
            source_page_url=url,
        )
        self.store.save_article(article)

    def test_dry_run_counts_blocked_host_articles(self) -> None:
        self._save_article("https://lib.ustc.edu.cn/valid/article.htm")
        self._save_article("https://career.lib.ustc.edu.cn/course/1.htm")
        self._save_article("https://mirror.lib.ustc.edu.cn/page/2.htm")
        result = self.store.cleanup_blocked_host_articles(source_id="library", commit=False)
        self.assertEqual(result["candidate_urls"], 2)
        self.assertEqual(result["removed_articles"], 0)
        self.assertTrue(result["dry_run"])
        count = self.store.db.execute(
            "SELECT COUNT(*) FROM articles WHERE source_id='library'"
        ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_commit_removes_blocked_host_articles_and_unlinks_media(self) -> None:
        good_url = "https://lib.ustc.edu.cn/valid/article.htm"
        bad_url = "https://career.lib.ustc.edu.cn/course/1.htm"
        self._save_article(good_url)
        self._save_article(bad_url)
        image = ImageRef(url="https://lib.ustc.edu.cn/img/a.png", article_url=bad_url)
        self.store.save_media(image, b"data", "image/png", bad_url, bad_url)
        result = self.store.cleanup_blocked_host_articles(source_id="library", commit=True)
        self.assertEqual(result["candidate_urls"], 1)
        self.assertEqual(result["removed_articles"], 1)
        self.assertFalse(result["dry_run"])
        self.assertIsNone(
            self.store.db.execute(
                "SELECT url FROM articles WHERE url=?", (bad_url,)
            ).fetchone()
        )
        self.assertIsNotNone(
            self.store.db.execute(
                "SELECT url FROM articles WHERE url=?", (good_url,)
            ).fetchone()
        )
        self.assertIsNone(
            self.store.db.execute(
                "SELECT article_url FROM article_media WHERE article_url=?", (bad_url,)
            ).fetchone()
        )
        media_row = self.store.db.execute(
            "SELECT article_url FROM media WHERE url=?", (image.url,)
        ).fetchone()
        self.assertIsNotNone(media_row)
        self.assertIsNone(media_row["article_url"])


class CleanupOrphanMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.db_path = self.data_dir / "crawler.sqlite"
        self.store = Store(self.db_path, self.data_dir)
        self.store.add_source(
            SourceConfig(
                id="news",
                name="新闻网",
                organization_level="university",
                seed_urls=["https://news.ustc.edu.cn/"],
                allowed_hosts=["news.ustc.edu.cn"],
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _save_article(self, url: str, images: list[ImageRef]) -> None:
        article = ArticleDocument(
            url=url,
            source_id="news",
            title="标题",
            author="",
            published_at="2026-08-01",
            updated_at="",
            category="",
            summary="摘要",
            body_html="<p>正文</p>",
            body_text="正文",
            body_markdown="正文",
            extraction_method="test",
            source_page_url=url,
            images=images,
        )
        self.store.save_article(article)

    def test_dry_run_counts_orphan_media(self) -> None:
        url = "https://news.ustc.edu.cn/article/1.htm"
        image = ImageRef(url="https://news.ustc.edu.cn/img/orphan.png", article_url=url)
        self._save_article(url, [image])
        self.store.save_media(image, b"data", "image/png", url, url)
        # Delete the article_media row but keep media, simulating stale linkage.
        self.store.db.execute("DELETE FROM article_media WHERE image_url=?", (image.url,))
        self.store.db.commit()
        result = self.store.cleanup_orphan_media(commit=False)
        self.assertEqual(result["orphan_media"], 1)
        self.assertEqual(result["stale_linkage"], 1)
        self.assertEqual(result["dangling_linkage"], 0)
        self.assertEqual(result["unreferenced"], 0)
        self.assertEqual(result["relinked"], 0)
        self.assertTrue(result["dry_run"])

    def test_commit_relinks_orphan_media_from_article_bundle(self) -> None:
        url = "https://news.ustc.edu.cn/article/1.htm"
        image = ImageRef(
            url="https://news.ustc.edu.cn/img/relink.png", alt="alt", article_url=url
        )
        self._save_article(url, [image])
        self.store.save_media(image, b"data", "image/png", url, url)
        self.store.db.execute("DELETE FROM article_media WHERE image_url=?", (image.url,))
        self.store.db.commit()
        result = self.store.cleanup_orphan_media(commit=True)
        self.assertEqual(result["orphan_media"], 1)
        self.assertEqual(result["stale_linkage"], 1)
        self.assertEqual(result["relinked"], 1)
        self.assertEqual(result["deleted"], 0)
        link = self.store.db.execute(
            "SELECT article_url, alt FROM article_media WHERE image_url=?", (image.url,)
        ).fetchone()
        self.assertIsNotNone(link)
        self.assertEqual(link["article_url"], url)
        self.assertEqual(link["alt"], "alt")

    def test_commit_deletes_unreferenced_orphan_media(self) -> None:
        url = "https://news.ustc.edu.cn/article/1.htm"
        image = ImageRef(
            url="https://news.ustc.edu.cn/img/unreferenced.png", article_url=url
        )
        self._save_article(url, [image])
        self.store.save_media(image, b"data", "image/png", url, url)
        # Remove both the article and its media link, leaving an unreferenced media row.
        self.store.db.execute("DELETE FROM article_media WHERE image_url=?", (image.url,))
        self.store.db.execute("UPDATE media SET article_url=NULL WHERE url=?", (image.url,))
        self.store.db.execute("DELETE FROM articles WHERE url=?", (url,))
        self.store.db.commit()
        result = self.store.cleanup_orphan_media(commit=True)
        self.assertEqual(result["orphan_media"], 1)
        self.assertEqual(result["unreferenced"], 1)
        self.assertEqual(result["relinked"], 0)
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(
            self.store.db.execute(
                "SELECT url FROM media WHERE url=?", (image.url,)
            ).fetchone()
        )


class CleanupExcessImagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.db_path = self.data_dir / "crawler.sqlite"
        self.store = Store(self.db_path, self.data_dir)
        self.store.add_source(
            SourceConfig(
                id="university",
                name="中国科学技术大学",
                organization_level="university",
                seed_urls=["https://www.ustc.edu.cn/"],
                allowed_hosts=["www.ustc.edu.cn"],
                max_images_per_page=2,
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_trim_excess_images_and_remove_new_orphans(self) -> None:
        url = "https://www.ustc.edu.cn/info/1/1.htm"
        images = [
            ImageRef(url=f"https://www.ustc.edu.cn/images/{index}.png", article_url=url)
            for index in range(4)
        ]
        article = ArticleDocument(
            url=url,
            source_id="university",
            title="标题",
            author="",
            published_at="2026-08-01",
            updated_at="",
            category="",
            summary="摘要",
            body_html="<p>正文</p>",
            body_text="正文",
            body_markdown="正文",
            extraction_method="test",
            source_page_url=url,
            images=images,
        )
        self.store.save_article(article)
        for image in images:
            self.store.save_media(image, b"data", "image/png", url, url)

        result = self.store.cleanup_data(
            commit=True,
            source_caps={"university": 2},
        )

        self.assertEqual(result["excess_images"]["deleted"], 2)
        self.assertEqual(result["orphan_media"]["deleted"], 2)
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM article_media WHERE article_url=?", (url,)
            ).fetchone()[0],
            2,
        )
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 2)

    def test_reindex_respects_source_image_cap(self) -> None:
        url = "https://www.ustc.edu.cn/info/1/2.htm"
        html = """<html><body><article><h1>图片新闻</h1>
        <time>发布时间：2026-08-01</time>
        <p>这是足够长的正文内容，用于验证离线重新提取不会恢复超过来源配置上限的图片关系。</p>
        <p>第二段正文确保页面可以被识别并保留为可索引的新闻文章。</p>
        <img src='/images/0.png'><img src='/images/1.png'>
        <img src='/images/2.png'><img src='/images/3.png'>
        </article></body></html>"""
        page = extract_page(url, html, source_id="university")
        self.assertIsNotNone(page.article)
        assert page.article is not None
        page.raw_body = html.encode()
        self.store.save_page(page, "university", 1)
        self.store.save_article(page.article)
        for image in page.article.images:
            self.store.save_media(image, b"data", "image/png", url, url)

        result = self.store.reindex_extractions(
            {"university"},
            {"university": 2},
        )

        self.assertEqual(result["articles"], 1)
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM article_media WHERE article_url=?", (url,)
            ).fetchone()[0],
            2,
        )


class CleanupCliTests(unittest.TestCase):
    def test_cli_dry_run_reports_counts(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            db_path = data_dir / "crawler.sqlite"
            store = Store(db_path, data_dir)
            store.add_source(
                SourceConfig(
                    id="library",
                    name="图书馆",
                    organization_level="service",
                    seed_urls=["https://lib.ustc.edu.cn/"],
                    allowed_hosts=["lib.ustc.edu.cn"],
                    blocked_hosts=["career.lib.ustc.edu.cn"],
                )
            )
            article = ArticleDocument(
                url="https://career.lib.ustc.edu.cn/course/1.htm",
                source_id="library",
                title="标题",
                author="",
                published_at="2026-08-01",
                updated_at="",
                category="",
                summary="摘要",
                body_html="<p>正文</p>",
                body_text="正文",
                body_markdown="正文",
                extraction_method="test",
                source_page_url="https://career.lib.ustc.edu.cn/course/1.htm",
            )
            store.save_article(article)
            store.close()
            rc = cli_main(
                [
                    "cleanup-data",
                    "--db",
                    str(db_path),
                    "--data-dir",
                    str(data_dir),
                    "--source",
                    "library",
                ]
            )
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
