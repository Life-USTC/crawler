import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support import store_core
from ustc_crawler.cli import main as cli_main
from ustc_crawler.extract import extract_page
from ustc_crawler.markdown import local_image_url
from ustc_crawler.models import ArticleDocument, ImageRef, PageDocument, SourceConfig
from ustc_crawler.store import Store, sha256_bytes
from ustc_crawler.sync.client import sync_backfill


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

    def test_reindex_preserves_all_image_sources(self) -> None:
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

        result = self.store.reindex_extractions({"university"}, page_urls={url})

        self.assertEqual(result["articles"], 1)
        self.assertEqual(
            store_core(self.store).execute(
                "SELECT COUNT(*) FROM article_media WHERE article_url=?", (url,)
            ).fetchone()[0],
            4,
        )

    def test_reindex_cursor_repairs_saved_html_and_enqueues_local_images(self) -> None:
        raw_by_url: dict[str, bytes] = {}
        urls = [
            "https://www.ustc.edu.cn/info/1/20.htm",
            "https://www.ustc.edu.cn/info/1/21.htm",
        ]
        for index, url in enumerate(urls):
            image_url = f"https://www.ustc.edu.cn/images/{index}.png"
            html = f"""<html><body><article><h1>图片新闻 {index}</h1>
            <time>发布时间：2026-08-01</time>
            <p>这是第 {index} 篇足够长的正文内容，用于验证历史 HTML 离线重处理能够生成新的本地图片 Markdown。</p>
            <p>正文段落还包含一个独特标记 historical-{index}，避免内容去重合并。</p>
            <p><img src='/images/{index}.png' alt='历史图片 {index}'></p>
            </article></body></html>"""
            raw = html.encode("utf-8")
            raw_by_url[url] = raw
            page = extract_page(url, html, source_id="university")
            self.assertIsNotNone(page.article)
            assert page.article is not None
            page.raw_body = raw
            self.store.save_page(page, "university", 1)
            page.article.body_markdown = f"![旧链接]({image_url})"
            self.store.save_article(page.article)

        raw_paths = {
            url: self.store._core.execute(
                "SELECT raw_path FROM pages WHERE url=?", (url,)
            ).fetchone()["raw_path"]
            for url in urls
        }
        before_raw = {
            url: Path(path).read_bytes()
            for url, path in raw_paths.items()
        }

        first = self.store.reindex_extractions({"university"}, limit=1)
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["articles"], 1)
        self.assertEqual(first["enqueued"], 1)
        first_row = self.store._core.execute(
            "SELECT body_html,body_markdown FROM articles WHERE url=?", (urls[0],)
        ).fetchone()
        self.assertIn("/images/0.png", first_row["body_html"])
        self.assertIn(local_image_url("https://www.ustc.edu.cn/images/0.png"), first_row["body_markdown"])
        self.assertIn("https://www.ustc.edu.cn/images/1.png", self.store._core.execute(
            "SELECT body_markdown FROM articles WHERE url=?", (urls[1],)
        ).fetchone()["body_markdown"])

        second = self.store.reindex_extractions(
            {"university"}, after_url=urls[0], limit=1
        )
        self.assertEqual(second["scanned"], 1)
        self.assertEqual(second["articles"], 1)
        self.assertEqual(second["enqueued"], 1)
        for url, path in raw_paths.items():
            self.assertEqual(Path(path).read_bytes(), before_raw[url])

        self.assertEqual(
            sync_backfill(self.store, chunk_size=2),
            {"scanned": 2, "enqueued": 0, "errors": 0},
        )
        rows = self.store._core.execute(
            "SELECT payload_json,object_manifest_json FROM sync_outbox ORDER BY entity_key"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            payload = json.loads(row["payload_json"])
            self.assertEqual(len(payload["imageSources"]), 1)
            self.assertNotIn("media", {item["kind"] for item in json.loads(row["object_manifest_json"])})

    def test_rebuild_markdown_uses_article_cursor_and_preserves_archived_fields(self) -> None:
        urls = [
            "https://www.ustc.edu.cn/info/1/30.htm",
            "https://www.ustc.edu.cn/info/1/31.htm",
        ]
        raw_paths: dict[str, Path] = {}
        before_rows: dict[str, dict[str, object]] = {}
        for index, url in enumerate(urls):
            image_url = f"https://www.ustc.edu.cn/images/repair-{index}.png"
            html = f"""<html><body><article><h1>Markdown 修复 {index}</h1>
            <time>发布时间：2026-08-01</time>
            <p>这是第 {index} 篇历史文章，用于验证从保存的 body HTML 重建 Markdown 时不会重新解析页面。</p>
            <p>第二段正文包含 repair-{index} 标记，确保两篇文章保持独立。</p>
            <p><img src='/images/repair-{index}.png' alt='修复图片 {index}'></p>
            </article></body></html>"""
            page = extract_page(url, html, source_id="university")
            self.assertIsNotNone(page.article)
            assert page.article is not None
            page.raw_body = html.encode("utf-8")
            raw_paths[url] = self.store.save_page(page, "university", 1)
            page.article.body_markdown = f"![旧链接]({image_url})"
            self.store.save_article(page.article)
            row = store_core(self.store).execute(
                "SELECT * FROM articles WHERE url=?", (url,)
            ).fetchone()
            before_rows[url] = dict(row)

        raw_before = {url: path.read_bytes() for url, path in raw_paths.items()}
        first = self.store.rebuild_markdown({"university"}, limit=1)
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["changed"], 1)
        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(first["last_url"], urls[0])

        second = self.store.rebuild_markdown(
            {"university"}, after_url=first["last_url"], limit=1
        )
        self.assertEqual(second["scanned"], 1)
        self.assertEqual(second["changed"], 1)
        self.assertEqual(second["enqueued"], 1)
        self.assertEqual(second["last_url"], urls[1])

        for url, path in raw_paths.items():
            self.assertEqual(path.read_bytes(), raw_before[url])
            row = store_core(self.store).execute(
                "SELECT * FROM articles WHERE url=?", (url,)
            ).fetchone()
            after = dict(row)
            self.assertEqual(after["body_html"], before_rows[url]["body_html"])
            self.assertEqual(after["body_text"], before_rows[url]["body_text"])
            self.assertEqual(after["title"], before_rows[url]["title"])
            self.assertNotEqual(after["body_markdown"], before_rows[url]["body_markdown"])
            self.assertIn(
                local_image_url(f"https://www.ustc.edu.cn/images/repair-{urls.index(url)}.png"),
                after["body_markdown"],
            )

        outbox_rows = store_core(self.store).execute(
            "SELECT payload_json,object_manifest_json FROM sync_outbox"
        ).fetchall()
        self.assertEqual(len(outbox_rows), 2)
        for row in outbox_rows:
            payload = json.loads(row["payload_json"])
            self.assertEqual(len(payload["imageSources"]), 1)
            self.assertNotIn(
                "media",
                {item["kind"] for item in json.loads(row["object_manifest_json"])},
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


class ReindexSchemeTwinTests(unittest.TestCase):
    """http/https twin pages must not delete each other's canonical article.

    set.ustc.edu.cn is reachable over both schemes and the archive holds one
    page row per scheme with identical bytes (same sha256).  Reindexing the
    http page saves the article at the normalized https URL while the stale
    http article row survives; the https twin page is then treated as a
    duplicate and its removal pass deleted the canonical https article again.
    """

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.db_path = self.data_dir / "crawler.sqlite"
        self.store = Store(self.db_path, self.data_dir)
        self.store.add_source(
            SourceConfig(
                id="set",
                name="未来技术学院",
                organization_level="college",
                seed_urls=["https://set.ustc.edu.cn/"],
                allowed_hosts=["set.ustc.edu.cn"],
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_scheme_twin_pages_keep_canonical_article_and_drop_http_orphan(self) -> None:
        http_url = "http://set.ustc.edu.cn/2026/0715/c35479a747698/page.htm"
        https_url = "https://set.ustc.edu.cn/2026/0715/c35479a747698/page.htm"
        html = """<html><head><title>瀚海讲堂：从爱因斯坦到量子计算机</title></head><body>
        <article><h1>瀚海讲堂：从爱因斯坦到量子计算机</h1>
        <time>发布时间：2026-07-15</time>
        <p>第一段足够长的正文内容，用于让离线重新提取把页面识别为一篇公开的新闻文章。</p>
        <p>第二段正文确保页面满足文章识别阈值，以验证 http 与 https 孪生页面的重新索引。</p>
        </article></body></html>"""
        http_page = extract_page(http_url, html, source_id="set")
        self.assertIsNotNone(http_page.article)
        http_page.raw_body = html.encode()
        self.store.save_page(http_page, "set", 1)
        # The stale pre-reindex state: an http article row with the old
        # column-name title, no https canonical row.
        self.store.save_article(
            ArticleDocument(
                url=http_url,
                source_id="set",
                title="通知公告 - 瀚海讲堂",
                author="",
                published_at="2026-07-15",
                updated_at="",
                category="",
                summary="",
                body_html="<p>正文</p>",
                body_text="正文",
                body_markdown="正文",
                extraction_method="test",
                source_page_url=http_url,
            )
        )
        https_page = extract_page(https_url, html, source_id="set")
        self.assertIsNotNone(https_page.article)
        https_page.raw_body = html.encode()
        self.store.save_page(https_page, "set", 1)

        result = self.store.reindex_extractions({"set"})

        self.assertEqual(result["articles"], 1)
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(result["removed"], 1)
        canonical = store_core(self.store).execute(
            "SELECT title FROM articles WHERE url=?", (https_url,)
        ).fetchone()
        self.assertIsNotNone(canonical)
        assert canonical is not None
        self.assertEqual(canonical["title"], "瀚海讲堂：从爱因斯坦到量子计算机")
        self.assertIsNone(
            store_core(self.store).execute(
                "SELECT url FROM articles WHERE url=?", (http_url,)
            ).fetchone()
        )
        tombstones = [
            json.loads(row["payload_json"])
            for row in store_core(self.store).execute("SELECT payload_json FROM sync_outbox")
        ]
        self.assertTrue(
            any(
                payload.get("tombstone") and payload.get("canonicalUrl") == http_url
                for payload in tombstones
            )
        )
        self.assertTrue(
            any(
                not payload.get("tombstone") and payload.get("canonicalUrl") == https_url
                for payload in tombstones
            )
        )


class ReindexContentDuplicateTests(unittest.TestCase):
    """Same-body pages saved before the #82 crawl-time content dedup.

    pnp's Indico renders one event at /event/N/, /event/N/overview and
    /event/N/?note=M with per-request markup, so the byte-level sha256 dedup
    never fires and the 2026-08 crawl (pre-#82) saved one article per URL.
    Reindexing must resolve these historical duplicates by content hash the
    same way the crawl path now does.
    """

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.db_path = self.data_dir / "crawler.sqlite"
        self.store = Store(self.db_path, self.data_dir)
        self.store.add_source(
            SourceConfig(
                id="pnp",
                name="核科学技术学院",
                organization_level="college",
                seed_urls=["https://indico.pnp.ustc.edu.cn/"],
                allowed_hosts=["indico.pnp.ustc.edu.cn"],
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _save_event_page(self, url: str, csrf: str, body_paragraphs: str) -> None:
        html = f"""<html><head><title>SCEP Weekly meeting @ 2023-12-26</title></head><body>
        <article data-csrf='{csrf}'><h1>SCEP Weekly meeting @ 2023-12-26</h1>
        <time>发布时间：2023-12-26</time>
        {body_paragraphs}
        </article></body></html>"""
        page = extract_page(url, html, source_id="pnp")
        self.assertIsNotNone(page.article)
        assert page.article is not None
        page.raw_body = html.encode()
        self.store.save_page(page, "pnp", 1)
        # The historical state: both URL variants already hold an article row.
        self.store.save_article(page.article)

    def test_reindex_merges_same_body_event_urls(self) -> None:
        bare_url = "https://indico.pnp.ustc.edu.cn/event/1278/"
        note_url = "https://indico.pnp.ustc.edu.cn/event/1278/?note=77"
        body = """
        <p>本周组会讨论超导量子比特读出链路的噪声来源，重点分析室温放大器引入的附加噪声对读出保真度的影响。</p>
        <p>会议确认了下一阶段的实验安排，包括低温链路的参数复测、读出谐振腔的带宽标定以及新一轮数据采集的时间窗口。</p>
        <p>与会人员还讨论了与合作单位共享数据的格式规范，决定沿用现有的事例记录模板并在下周例会前完成文档更新。</p>
        <p>最后安排了值周报告顺序，要求每位报告人提前一天将幻灯片上传到组内共享目录以便会前审阅，并在报告结束后及时整理会议纪要。</p>
        """
        # Per-request markup differs (csrf attribute), so the raw bytes -
        # and therefore the sha256 page dedup - never match.
        self._save_event_page(bare_url, "csrf-a", body)
        self._save_event_page(note_url, "csrf-b", body)

        result = self.store.reindex_extractions({"pnp"})

        self.assertEqual(result["content_duplicates"], 1)
        self.assertEqual(result["articles"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertIsNotNone(
            store_core(self.store).execute(
                "SELECT url FROM articles WHERE url=?", (bare_url,)
            ).fetchone()
        )
        self.assertIsNone(
            store_core(self.store).execute(
                "SELECT url FROM articles WHERE url=?", (note_url,)
            ).fetchone()
        )
        duplicate = store_core(self.store).execute(
            "SELECT duplicate_of FROM pages WHERE url=?", (note_url,)
        ).fetchone()
        self.assertEqual(duplicate["duplicate_of"], bare_url)
        tombstones = [
            json.loads(row["payload_json"])
            for row in store_core(self.store).execute("SELECT payload_json FROM sync_outbox")
        ]
        self.assertTrue(
            any(
                payload.get("tombstone") and payload.get("canonicalUrl") == note_url
                for payload in tombstones
            )
        )

    def test_reindex_content_dedup_ignores_short_bodies(self) -> None:
        # Indico contribution pages share a tiny boilerplate body ("报告人：…"
        # below the 200-char floor) while remaining distinct talks; merging
        # them by content hash would delete real pages.  The crawl-time floor
        # applies to reindexing too.
        first_url = "https://indico.pnp.ustc.edu.cn/event/2562/contributions/14687/"
        second_url = "https://indico.pnp.ustc.edu.cn/event/2562/contributions/14688/"
        body = "<p>报告人：张三。地点：物质科研楼C座。</p>"
        self._save_event_page(first_url, "csrf-a", body)
        self._save_event_page(second_url, "csrf-b", body)

        result = self.store.reindex_extractions({"pnp"})

        self.assertEqual(result["content_duplicates"], 0)
        self.assertEqual(result["articles"], 2)
        for url in (first_url, second_url):
            self.assertIsNotNone(
                store_core(self.store).execute(
                    "SELECT url FROM articles WHERE url=?", (url,)
                ).fetchone()
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
