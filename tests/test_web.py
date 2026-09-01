import json
import threading
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
from urllib.request import urlopen

from ustc_crawler.models import SourceConfig
from ustc_crawler.store import Store
from ustc_crawler.web import DashboardHTTPServer, DashboardStore, _safe_article_html


class DashboardTests(unittest.TestCase):
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
        today = date.today().isoformat()
        store.db.execute(
            """
            INSERT INTO pages(
              url,source_id,final_url,status,content_type,fetched_at,depth,discovered_from,
              sha256,raw_path,title,canonical_url,error,blocked_by_robots,page_kind,access_mode,
              value_score,value_tier,score_reasons,published_at,duplicate_of
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "https://news.example.test/article/1",
                "news",
                "https://news.example.test/article/1",
                200,
                "text/html",
                today,
                0,
                "",
                "hash",
                "data/pages/hash.html",
                "测试新闻",
                "https://news.example.test/article/1",
                "",
                0,
                "news_article",
                "public",
                100,
                "full_index",
                "[]",
                today,
                "",
            ),
        )
        store.db.execute(
            """
            INSERT INTO articles(
              url,source_id,title,author,published_at,updated_at,category,summary,body_html,
              body_text,body_markdown,extraction_method,source_page_url,raw_json,content_hash,
              first_seen,last_seen
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "https://news.example.test/article/1",
                "news",
                "测试新闻",
                "记者",
                today,
                "",
                "通知",
                "摘要",
                "<p>正文</p>",
                "正文内容",
                "正文内容",
                "test",
                "https://news.example.test/article/1",
                "{}",
                "article-hash",
                today,
                today,
            ),
        )
        store.db.execute(
            """
            INSERT INTO articles(
              url,source_id,title,author,published_at,updated_at,category,summary,body_html,
              body_text,body_markdown,extraction_method,source_page_url,raw_json,content_hash,
              first_seen,last_seen
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "https://news.example.test/article/duplicate",
                "news",
                "重复新闻",
                "记者",
                today,
                "",
                "通知",
                "摘要",
                "<p>重复正文</p>",
                "重复正文内容",
                "重复正文内容",
                "test",
                "https://news.example.test/article/duplicate",
                "{}",
                "article-hash",
                today,
                today,
            ),
        )
        image_path = self.data_dir / "media" / "aa" / "image.png"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"PNG")
        store.db.execute(
            "INSERT INTO media(url,article_url,source_page_url,local_path,mime_type,sha256,size,status,fetched_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "https://news.example.test/image.png",
                "https://news.example.test/article/1",
                "https://news.example.test/article/1",
                "data/media/aa/image.png",
                "image/png",
                "image-hash",
                3,
                "ok",
                today,
            ),
        )
        store.db.execute(
            "INSERT INTO article_media(article_url,image_url,local_path,alt,title,caption,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                "https://news.example.test/article/1",
                "https://news.example.test/image.png",
                "data/media/aa/image.png",
                "配图",
                "",
                "",
                today,
            ),
        )
        asset_path = self.data_dir / "assets" / "aa" / "assignment.pdf"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"PDF")
        store.db.execute(
            "INSERT INTO assets(url,source_url,local_path,mime_type,size,status,fetched_at,page_kind,access_mode,value_score,score_reasons) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "https://news.example.test/assignment.pdf",
                "https://news.example.test/article/1",
                "data/assets/aa/assignment.pdf",
                "application/pdf",
                3,
                "ok",
                today,
                "document",
                "public",
                25,
                "[]",
            ),
        )
        store.db.commit()
        store.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_queries_expose_summary_news_and_local_files(self) -> None:
        dashboard = DashboardStore(self.db_path, self.data_dir)
        summary = dashboard.summary()
        self.assertEqual(summary["sources"], 1)
        self.assertEqual(summary["news_indexable"], 1)
        self.assertEqual(summary["assets"], 1)
        self.assertEqual(dashboard.news()[0][0]["title"], "测试新闻")
        self.assertEqual(dashboard.news()[0][0]["publication_type"], "notice")
        self.assertEqual(dashboard.news()[1], 1)
        self.assertEqual(dashboard.news(publication_type="notice")[1], 1)
        self.assertEqual(dashboard.news(publication_type="news")[1], 0)
        self.assertEqual(len(dashboard.sources()), 1)
        self.assertEqual(
            dashboard.local_path("data/media/aa/image.png", "media"),
            self.data_dir / "media/aa/image.png",
        )
        self.assertIsNone(dashboard.local_path("../crawler.sqlite", "media"))

    def test_publication_type_uses_section_metadata_without_broad_title_matches(self) -> None:
        store = Store(self.db_path, self.data_dir)
        today = date.today().isoformat()

        def add(url: str, title: str, page_kind: str, discovered_from: str = "") -> None:
            store.db.execute(
                """
                INSERT INTO pages(
                  url,source_id,final_url,status,content_type,fetched_at,depth,discovered_from,
                  sha256,raw_path,title,canonical_url,error,blocked_by_robots,page_kind,access_mode,
                  value_score,value_tier,score_reasons,published_at,duplicate_of
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    url, "news", url, 200, "text/html", today, 1, discovered_from,
                    "hash", "data/pages/hash.html", title, url, "", 0, page_kind, "public",
                    100, "full_index", "[]", today, "",
                ),
            )
            store.db.execute(
                """
                INSERT INTO articles(
                  url,source_id,title,author,published_at,updated_at,category,summary,body_html,
                  body_text,body_markdown,extraction_method,source_page_url,raw_json,content_hash,
                  first_seen,last_seen
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (url, "news", title, "", today, "", "", "", "<p>正文</p>", "正文", "正文",
                 "test", url, "{}", url, today, today),
            )

        add("https://news.example.test/item/award", "科学探索奖获奖名单出炉", "news_article")
        add("https://news.example.test/item/admission", "颁发首批录取通知书", "news_article")
        add("https://news.example.test/item/section", "本周工作动态", "news_article", "https://news.example.test/tzgg/list.htm")
        add("https://news.example.test/item/bus", "校园班车运行时刻表", "news_article")
        add("https://news.example.test/course/1", "课程资料", "course_resource")
        store.db.execute(
            "UPDATE articles SET published_at='' WHERE url='https://news.example.test/item/bus'"
        )
        store.db.commit()
        store.close()

        dashboard = DashboardStore(self.db_path, self.data_dir)
        all_rows, all_total, _ = dashboard.news(page_size=100)
        news_rows, news_total, _ = dashboard.news(publication_type="news", page_size=100)
        notice_rows, notice_total, _ = dashboard.news(publication_type="notice", page_size=100)
        self.assertEqual(all_total, news_total + notice_total)
        self.assertEqual(len(all_rows), all_total)
        self.assertEqual(news_total, 2)
        self.assertEqual(notice_total, 3)
        self.assertEqual(
            {row["title"] for row in news_rows},
            {"科学探索奖获奖名单出炉", "颁发首批录取通知书"},
        )
        self.assertEqual(
            {row["title"] for row in notice_rows},
            {"测试新闻", "本周工作动态", "校园班车运行时刻表"},
        )
        self.assertNotIn("课程资料", {row["title"] for row in news_rows + notice_rows})

    def test_http_routes_render_html_and_json(self) -> None:
        server = DashboardHTTPServer(("127.0.0.1", 0), DashboardStore(self.db_path, self.data_dir))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{base}/") as response:
                self.assertEqual(response.status, 200)
                self.assertIn("USTC Crawl Explorer", response.read().decode("utf-8"))
            with urlopen(f"{base}/api/summary") as response:
                payload = json.load(response)
                self.assertEqual(payload["pages"], 1)
            with urlopen(f"{base}/news?type=notice") as response:
                rendered = response.read().decode("utf-8")
                self.assertIn("仅通知", rendered)
                self.assertIn('<span class="tag notice">通知</span>', rendered)
            with urlopen(f"{base}/api/news?type=notice") as response:
                payload = json.load(response)
                self.assertEqual(payload["total"], 1)
                self.assertEqual(payload["items"][0]["publication_type"], "notice")
            url = quote("https://news.example.test/article/1", safe="")
            with urlopen(f"{base}/article?url={url}") as response:
                rendered = response.read().decode("utf-8")
                self.assertIn("<div class=\"article-body\"><p>正文</p>", rendered)
            with urlopen(f"{base}/media?path={quote('data/media/aa/image.png', safe='')}") as response:
                self.assertEqual(response.read(), b"PNG")
            with urlopen(f"{base}/asset?path={quote('data/assets/aa/assignment.pdf', safe='')}") as response:
                self.assertEqual(response.read(), b"PDF")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_sanitizer_handles_detached_descendants(self) -> None:
        rendered = _safe_article_html(
            {
                "url": "https://news.example.test/article/1",
                "body_html": "<script><span>drop me</span></script><p>keep me</p>",
                "images": [],
            }
        )
        self.assertEqual(rendered, "<p>keep me</p>")


if __name__ == "__main__":
    unittest.main()
