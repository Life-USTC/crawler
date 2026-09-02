import json
import tempfile
import unittest
from pathlib import Path

from ustc_crawler.media import _image_jobs
from ustc_crawler.store import article_bundle_path


class _Store:
    def __init__(self, data_dir: Path, article_url: str) -> None:
        self.data_dir = data_dir
        self.article_url = article_url

    def article_media_records(self) -> list[dict[str, str]]:
        return [
            {
                "article_url": self.article_url,
                "image_url": "https://example.ustc.edu.cn/first.jpg",
                "alt": "first",
                "title": "",
                "caption": "",
            }
        ]

    def article_records_for_media(self) -> list[dict[str, str]]:
        return [{"url": self.article_url, "content_hash": "digest"}]


class MediaJobTests(unittest.TestCase):
    def test_partial_article_media_is_completed_from_bundle(self) -> None:
        article_url = "https://example.ustc.edu.cn/info/1/2.htm"
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            bundle = article_bundle_path(data_dir, article_url)
            bundle.parent.mkdir(parents=True)
            bundle.write_text(
                json.dumps(
                    {
                        "images": [
                            {"url": "https://example.ustc.edu.cn/first.jpg", "alt": "first"},
                            {"url": "https://example.ustc.edu.cn/second.jpg", "alt": "second"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            jobs = _image_jobs(_Store(data_dir, article_url))  # type: ignore[arg-type]

        self.assertEqual(set(jobs), {
            "https://example.ustc.edu.cn/first.jpg",
            "https://example.ustc.edu.cn/second.jpg",
        })
        self.assertEqual(len(jobs["https://example.ustc.edu.cn/first.jpg"]), 1)
        self.assertEqual(jobs["https://example.ustc.edu.cn/second.jpg"][0].article_url, article_url)
