import unittest
from pathlib import Path

from ustc_crawler.adapters.course import CourseAdapter


class CourseAdapterTests(unittest.TestCase):
    def test_course_fixture(self) -> None:
        html = Path("tests/fixtures/adapters/course/course.html").read_text(
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


if __name__ == "__main__":
    unittest.main()
