from __future__ import annotations

import sqlite3
import unittest

from ustc_crawler.publication import (
    CLASSIFIER_VERSION,
    classify_publication,
    publication_type_sql,
)


class PublicationClassifierTests(unittest.TestCase):
    def test_non_publication_course_video_and_profile_pages_are_other(self) -> None:
        cases = (
            {"url": "https://sz.ustc.edu.cn/kecheng/video/detail_87_0.htm"},
            {"url": "https://math.ustc.edu.cn/2024/1205/c1a2/page.htm", "title": "Faculty"},
            {"url": "https://emba.ustc.edu.cn/2025/1209/c1a2/page.htm", "category": "视频中心"},
            {
                "url": "https://math.ustc.edu.cn/2024/1205/c1a2/page.htm",
                "source_id": "unit-math-ustc-edu-cn",
                "title": "Yu Shucheng",
                "category": "Fundamental Mathematics",
            },
        )
        for values in cases:
            with self.subTest(values=values):
                self.assertEqual(classify_publication(**values), "other")

    def test_administrative_admissions_titles_are_notices(self) -> None:
        titles = (
            "2026年招生导师及研究方向",
            "2026年招收硕士研究生复试办法及流程",
            "2026年申请考核制博士报名通告",
            "硕士复试和录取补充规定",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(
                    classify_publication(url="https://example.edu/news/1", title=title),
                    "notice",
                )

    def test_generic_notice_title_and_existing_exclusions(self) -> None:
        self.assertEqual(
            classify_publication(url="https://example.edu/news/1", title="校内通告"),
            "notice",
        )
        for title in ("录取通知书", "招生工作新闻报道", "复试录取新闻报道"):
            with self.subTest(title=title):
                self.assertEqual(
                    classify_publication(url="https://example.edu/news/1", title=title),
                    "news",
                )

    def test_sql_fallback_matches_python_classifier(self) -> None:
        titles = (
            "2026年招生导师及研究方向",
            "2026年招收硕士研究生复试办法及流程",
            "2026年申请考核制博士报名通告",
            "硕士复试和录取补充规定",
            "录取通知书",
            "招生工作新闻报道",
            "复试录取新闻报道",
            "Faculty",
        )
        expression = publication_type_sql()
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(
                """
                CREATE TABLE articles (
                  url TEXT PRIMARY KEY,
                  source_id TEXT,
                  title TEXT,
                  category TEXT,
                  source_page_url TEXT,
                  publication_type TEXT
                );
                CREATE TABLE pages (
                  url TEXT PRIMARY KEY,
                  final_url TEXT,
                  canonical_url TEXT,
                  discovered_from TEXT,
                  page_kind TEXT
                );
                """
            )
            for index, title in enumerate(titles):
                url = f"https://example.edu/news/{index}"
                connection.execute(
                    "INSERT INTO articles VALUES (?, ?, ?, ?, ?, '')",
                    (url, "source", title, "", url),
                )
                connection.execute(
                    "INSERT INTO pages VALUES (?, '', '', '', 'news_article')", (url,)
                )
            rows = connection.execute(
                f"SELECT a.title, {expression} AS publication_type "
                "FROM articles a JOIN pages p ON p.url=a.url ORDER BY a.url"
            ).fetchall()

        for title, publication_type in rows:
            with self.subTest(title=title):
                self.assertEqual(
                    publication_type,
                    classify_publication(url="https://example.edu/news/1", title=title),
                )

    def test_classifier_version_is_bumped_for_new_rules(self) -> None:
        self.assertEqual(CLASSIFIER_VERSION, "publication-v3")


if __name__ == "__main__":
    unittest.main()
