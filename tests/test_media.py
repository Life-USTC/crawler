import json
import tempfile
import unittest
from pathlib import Path

from ustc_crawler.cli import build_parser
from ustc_crawler.media import _image_jobs, _job_priority
from ustc_crawler.store import article_bundle_path


class _Store:
    def __init__(self, data_dir: Path, article_url: str) -> None:
        self.data_dir = data_dir
        self.article_url = article_url
        self.requested_source_ids: list[set[str] | None] = []

    def article_media_records(self, source_ids: set[str] | None = None) -> list[dict[str, str]]:
        self.requested_source_ids.append(source_ids)
        return [
            {
                "article_url": self.article_url,
                "image_url": "https://example.ustc.edu.cn/first.jpg",
                "alt": "first",
                "title": "",
                "caption": "",
            }
        ]

    def article_records_for_media(
        self, source_ids: set[str] | None = None
    ) -> list[dict[str, str]]:
        self.requested_source_ids.append(source_ids)
        return [{"url": self.article_url, "content_hash": "digest"}]


class MediaJobTests(unittest.TestCase):
    def test_unseen_media_is_prioritized_before_retries_and_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "image.jpg"
            local_path.write_bytes(b"image")

            self.assertEqual(_job_priority(None), 0)
            self.assertEqual(
                _job_priority({"status": "error", "local_path": ""}),
                1,
            )
            self.assertEqual(
                _job_priority({"status": "ok", "local_path": str(local_path)}),
                2,
            )

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

            store = _Store(data_dir, article_url)
            jobs = _image_jobs(store, {"unit-example"})  # type: ignore[arg-type]

        self.assertEqual(set(jobs), {
            "https://example.ustc.edu.cn/first.jpg",
            "https://example.ustc.edu.cn/second.jpg",
        })
        self.assertEqual(len(jobs["https://example.ustc.edu.cn/first.jpg"]), 1)
        self.assertEqual(jobs["https://example.ustc.edu.cn/second.jpg"][0].article_url, article_url)
        self.assertEqual(store.requested_source_ids, [{"unit-example"}, {"unit-example"}])

    def test_cli_accepts_repeated_source_filters(self) -> None:
        args = build_parser().parse_args(
            ["download-images", "--source", "university", "--source", "unit-soe"]
        )
        self.assertEqual(args.source, ["university", "unit-soe"])
