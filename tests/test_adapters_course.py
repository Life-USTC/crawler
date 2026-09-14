import unittest
from pathlib import Path

from ustc_crawler.adapters.course import CourseAdapter


class CourseAdapterTests(unittest.TestCase):
    def test_course_fixture(self) -> None:
        html = (Path(__file__).parent / "fixtures/adapters/course/course.html").read_text(
            encoding="utf-8"
        )
        fields = CourseAdapter().extract(
            "https://course.ustc.edu.cn/portal/news/info?id=19", html
        )
        self.assertIsNotNone(fields)
        self.assertEqual(
            fields.title, "关于中国科学技术大学“瀚海教学网”上线试运行的通知"
        )
        self.assertEqual(fields.published_at, "")
        self.assertIn("瀚海教学网", fields.body_html)

    def test_other_path_returns_none(self) -> None:
        self.assertIsNone(
            CourseAdapter().extract(
                "https://course.ustc.edu.cn/portal",
                "<html><body><p>首页</p></body></html>",
            )
        )

    def test_date_in_infion_con_is_extracted(self) -> None:
        html = (
            "<html><body><div class='infion-con'>"
            "<a href='/portal/news/notice' class='fr'>返回</a>"
            "<span>平台维护通知标题 </span>"
            "<em>发布时间：2026-09-01</em></div>"
            "<div class='Content'><p>正文内容</p></div>"
            "</body></html>"
        )
        fields = CourseAdapter().extract(
            "https://course.ustc.edu.cn/portal/news/info?id=20", html
        )
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.title, "平台维护通知标题")
        self.assertEqual(fields.published_at, "2026-09-01")


if __name__ == "__main__":
    unittest.main()
