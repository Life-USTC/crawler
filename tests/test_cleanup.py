import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support import store_core
from ustc_crawler.cli import main as cli_main
from ustc_crawler.extract import extract_page
from ustc_crawler.models import ArticleDocument, ImageRef, PageDocument, SourceConfig
from ustc_crawler.store import Store, sha256_bytes


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
        count = store_core(self.store).execute(
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
            store_core(self.store).execute(
                "SELECT url FROM articles WHERE url=?", (bad_url,)
            ).fetchone()
        )
        self.assertIsNotNone(
            store_core(self.store).execute(
                "SELECT url FROM articles WHERE url=?", (good_url,)
            ).fetchone()
        )
        self.assertIsNone(
            store_core(self.store).execute(
                "SELECT article_url FROM article_media WHERE article_url=?", (bad_url,)
            ).fetchone()
        )
        media_row = store_core(self.store).execute(
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
        store_core(self.store).execute("DELETE FROM article_media WHERE image_url=?", (image.url,))
        store_core(self.store).commit()
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
        store_core(self.store).execute("DELETE FROM article_media WHERE image_url=?", (image.url,))
        store_core(self.store).commit()
        result = self.store.cleanup_orphan_media(commit=True)
        self.assertEqual(result["orphan_media"], 1)
        self.assertEqual(result["stale_linkage"], 1)
        self.assertEqual(result["relinked"], 1)
        self.assertEqual(result["deleted"], 0)
        link = store_core(self.store).execute(
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
        store_core(self.store).execute("DELETE FROM article_media WHERE image_url=?", (image.url,))
        store_core(self.store).execute("UPDATE media SET article_url=NULL WHERE url=?", (image.url,))
        store_core(self.store).execute("DELETE FROM articles WHERE url=?", (url,))
        store_core(self.store).commit()
        result = self.store.cleanup_orphan_media(commit=True)
        self.assertEqual(result["orphan_media"], 1)
        self.assertEqual(result["unreferenced"], 1)
        self.assertEqual(result["relinked"], 0)
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(
            store_core(self.store).execute(
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
            store_core(self.store).execute(
                "SELECT COUNT(*) FROM article_media WHERE article_url=?", (url,)
            ).fetchone()[0],
            2,
        )
        self.assertEqual(store_core(self.store).execute("SELECT COUNT(*) FROM media").fetchone()[0], 2)

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
            store_core(self.store).execute(
                "SELECT COUNT(*) FROM article_media WHERE article_url=?", (url,)
            ).fetchone()[0],
            2,
        )

    def test_reindex_drops_article_after_redirect_to_unowned_host(self) -> None:
        requested_url = "https://www.ustc.edu.cn/info/1/3.htm"
        final_url = "https://outside.example.invalid/article/3.htm"
        html = """<html><body><article><h1>重定向页面</h1>
        <time>发布时间：2026-08-01</time>
        <p>这是足够长的正文内容，用于验证离线重新提取不会把外部重定向页面归档为本来源文章。</p>
        <p>第二段正文确保页面满足文章识别阈值，但来源主机不在允许列表中。</p>
        </article></body></html>"""
        page = extract_page(final_url, html, source_id="university")
        self.assertIsNotNone(page.article)
        assert page.article is not None
        page.requested_url = requested_url
        page.raw_body = html.encode()
        self.store.save_page(page, "university", 1)
        self.store.save_article(page.article)

        result = self.store.reindex_extractions(page_urls={requested_url})

        self.assertEqual(result["removed"], 1)
        self.assertIsNone(
            store_core(self.store).execute(
                "SELECT url FROM articles WHERE url=?", (final_url,)
            ).fetchone()
        )
        tombstone = store_core(self.store).execute(
            "SELECT payload_json FROM sync_outbox ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(tombstone)
        self.assertTrue(json.loads(tombstone["payload_json"])["tombstone"])

    def test_reindex_repairs_binary_document_mislabeled_as_html(self) -> None:
        url = "https://www.ustc.edu.cn/files/slides.pptx"
        page = PageDocument(
            requested_url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            fetched_at="2026-08-01T00:00:00+08:00",
            title="PK corrupted binary title",
            canonical_url=url,
            html="PK corrupted binary body",
            links=[],
            images=[],
            raw_body=b"PK\x03\x04office document",
        )
        self.store.save_page(page, "university", 1)

        self.store.reindex_extractions(page_urls={url})

        repaired = store_core(self.store).execute(
            "SELECT title,page_kind,value_tier FROM pages WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(repaired["title"], "slides.pptx")
        self.assertEqual(repaired["page_kind"], "document")
        self.assertEqual(repaired["value_tier"], "not_indexed")

    def test_reindex_removes_uploaded_html_attachment_article(self) -> None:
        url = (
            "https://www.ustc.edu.cn/_upload/article/files/7d/f9/"
            "033cd3b84a9d8a16b2b2eb9987e6/W020150417520333865223.htm"
        )
        html = """<html><body><article><h1>教程附件</h1>
        <time>发布时间：2026-08-01</time>
        <p>这是一个足够长的 HTML 附件正文，用于验证静态附件不会被重新提取为新闻文章。</p>
        <p>第二段内容让旧版提取结果满足文章阈值，但重新索引必须把它归档为附件。</p>
        </article></body></html>"""
        page = extract_page(url, html, source_id="university")
        self.assertIsNotNone(page.article)
        assert page.article is not None
        page.raw_body = html.encode()
        self.store.save_page(page, "university", 1)
        self.store.save_article(page.article)

        result = self.store.reindex_extractions(page_urls={url})

        repaired = store_core(self.store).execute(
            "SELECT title,page_kind,value_tier,raw_path FROM pages WHERE url=?", (url,)
        ).fetchone()
        article = store_core(self.store).execute(
            "SELECT url FROM articles WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(result["removed"], 1)
        self.assertEqual(repaired["title"], "W020150417520333865223.htm")
        self.assertEqual(repaired["page_kind"], "document")
        self.assertEqual(repaired["value_tier"], "not_indexed")
        self.assertTrue(repaired["raw_path"])
        self.assertIsNone(article)
        tombstone = store_core(self.store).execute(
            "SELECT payload_json FROM sync_outbox ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(tombstone)
        self.assertEqual(json.loads(tombstone["payload_json"])["canonicalUrl"], url)
        self.assertTrue(json.loads(tombstone["payload_json"])["tombstone"])

    def test_reindex_removes_article_with_attached_media(self) -> None:
        url = (
            "https://www.ustc.edu.cn/_upload/article/files/7d/f9/"
            "033cd3b84a9d8a16b2b2eb9987e6/W020150417520333865224.htm"
        )
        html = """<html><body><article><h1>带图附件</h1>
        <time>发布时间：2026-08-01</time>
        <p>这是一个足够长的 HTML 附件正文，用于验证带图片的静态附件在重新索引时能被干净移除。</p>
        <p>第二段内容让旧版提取结果满足文章阈值，但重新索引必须把它归档为附件。</p>
        <img src='/images/0.png'>
        </article></body></html>"""
        page = extract_page(url, html, source_id="university")
        self.assertIsNotNone(page.article)
        assert page.article is not None
        page.raw_body = html.encode()
        self.store.save_page(page, "university", 1)
        self.store.save_article(page.article)
        for image in page.article.images:
            self.store.save_media(image, b"data", "image/png", url, url)

        result = self.store.reindex_extractions(page_urls={url})

        self.assertEqual(result["removed"], 1)
        self.assertIsNone(
            store_core(self.store).execute(
                "SELECT url FROM articles WHERE url=?", (url,)
            ).fetchone()
        )
        self.assertEqual(
            store_core(self.store).execute(
                "SELECT COUNT(*) FROM media WHERE article_url=?", (url,)
            ).fetchone()[0],
            0,
        )


class RetextArticleBodiesTests(unittest.TestCase):
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

    def _save(self, url: str, body_html: str, body_text: str) -> None:
        self.store.save_article(
            ArticleDocument(
                url=url,
                source_id="library",
                title="通知",
                author="",
                published_at="2026-09-01",
                updated_at="",
                category="",
                summary="",
                body_html=body_html,
                body_text=body_text,
                body_markdown="",
                extraction_method="test",
                source_page_url=url,
            )
        )

    def test_retext_joins_inline_spans_and_updates_hash(self) -> None:
        url = "https://lib.ustc.edu.cn/2026/0901/c1a2/page.htm"
        self._save(
            url,
            "<div><p><span>根据省保健委</span><span>《关于做好<span>2026</span>"
            "年度健康体检工作的通知》，</span><span>我校现开展体检工作。</span></p></div>",
            "根据省保健委\n《关于做好\n2026\n年度健康体检工作的通知》，\n我校现开展体检工作。",
        )

        result = self.store.retext_article_bodies()

        self.assertEqual(result, {"scanned": 1, "changed": 1})
        row = store_core(self.store).execute(
            "SELECT body_text,content_hash FROM articles WHERE url=?", (url,)
        ).fetchone()
        self.assertEqual(
            row["body_text"], "根据省保健委《关于做好2026年度健康体检工作的通知》，我校现开展体检工作。"
        )
        self.assertEqual(
            row["content_hash"], sha256_bytes(row["body_text"].encode("utf-8"))
        )

    def test_retext_leaves_clean_text_untouched(self) -> None:
        url = "https://lib.ustc.edu.cn/2026/0901/c1a3/page.htm"
        self._save(url, "<div><p>各有关单位：</p><p>请知悉。</p></div>", "各有关单位：\n请知悉。")
        before = store_core(self.store).execute(
            "SELECT content_hash FROM articles WHERE url=?", (url,)
        ).fetchone()["content_hash"]

        result = self.store.retext_article_bodies()

        self.assertEqual(result, {"scanned": 1, "changed": 0})
        after = store_core(self.store).execute(
            "SELECT content_hash FROM articles WHERE url=?", (url,)
        ).fetchone()["content_hash"]
        self.assertEqual(before, after)


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
