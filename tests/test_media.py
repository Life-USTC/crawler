import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import ustc_crawler.media as media_module
from ustc_crawler.cli import build_parser
from ustc_crawler.media import (
    MediaOptions,
    _image_jobs,
    _interleave_jobs_by_host,
    _job_priority,
    download_saved_images,
)
from ustc_crawler.models import FetchResponse
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

    def test_torn_ok_record_with_size_mismatch_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "image.jpg"
            local_path.write_bytes(b"image")

            self.assertEqual(
                _job_priority(
                    {"status": "ok", "local_path": str(local_path), "size": 999}
                ),
                1,
            )
            self.assertEqual(
                _job_priority(
                    {"status": "ok", "local_path": str(local_path), "size": 5}
                ),
                2,
            )

    def test_ok_record_with_unknown_size_is_trusted(self) -> None:
        # Legacy rows can carry size=0; treat an unknown size as intact
        # rather than re-downloading the whole archive.
        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "image.jpg"
            local_path.write_bytes(b"image")

            self.assertEqual(
                _job_priority({"status": "ok", "local_path": str(local_path), "size": 0}),
                2,
            )
            self.assertEqual(
                _job_priority({"status": "ok", "local_path": str(local_path)}),
                2,
            )

    def test_media_jobs_round_robin_hosts_within_priority(self) -> None:
        retry = {"status": "error", "local_path": ""}
        planned = [
            (0, "https://a.ustc.edu.cn/1.jpg", [], None),
            (0, "https://a.ustc.edu.cn/2.jpg", [], None),
            (0, "https://a.ustc.edu.cn/3.jpg", [], None),
            (0, "https://b.ustc.edu.cn/1.jpg", [], None),
            (0, "https://b.ustc.edu.cn/2.jpg", [], None),
            (1, "https://c.ustc.edu.cn/retry.jpg", [], retry),
        ]

        result = _interleave_jobs_by_host(planned)

        self.assertEqual(
            [url for _, url, _, _ in result],
            [
                "https://a.ustc.edu.cn/1.jpg",
                "https://b.ustc.edu.cn/1.jpg",
                "https://a.ustc.edu.cn/2.jpg",
                "https://b.ustc.edu.cn/2.jpg",
                "https://a.ustc.edu.cn/3.jpg",
                "https://c.ustc.edu.cn/retry.jpg",
            ],
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


class _FakeStore:
    instances: list["_FakeStore"] = []

    def __init__(self, db_path: str, data_dir: str) -> None:
        self.save_calls: list[tuple] = []
        self.link_calls: list[tuple] = []
        self.threads: set[int] = set()
        self.closed = False
        self.jobs_error: Exception | None = None
        self.existing = None
        _FakeStore.instances.append(self)

    def article_media_records(self, source_ids=None):
        if self.jobs_error is not None:
            raise self.jobs_error
        return [
            {
                "article_url": "https://a.test/article/1",
                "image_url": "https://i.test/x.jpg",
                "alt": "",
                "title": "",
                "caption": "",
            }
        ]

    def article_records_for_media(self, source_ids=None):
        return []

    def media_snapshot(self, url):
        return self.existing

    def save_media(self, *args):
        self.threads.add(threading.get_ident())
        self.save_calls.append(args)

    def link_media(self, *args):
        self.threads.add(threading.get_ident())
        self.link_calls.append(args)

    def close(self):
        self.closed = True


class _FakeFetcher:
    instances: list["_FakeFetcher"] = []
    response: FetchResponse | None = None
    init_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        _FakeFetcher.init_kwargs = kwargs
        self.closed = False
        _FakeFetcher.instances.append(self)

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResponse:
        return _FakeFetcher.response

    async def close(self):
        self.closed = True


def _response(status: int, body: bytes = b"", error: str = "") -> FetchResponse:
    return FetchResponse(
        requested_url="https://i.test/x.jpg",
        final_url="https://i.test/x.jpg",
        status=status,
        content_type="image/jpeg",
        headers={},
        body=body,
        error=error,
    )


class MediaRunTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeStore.instances = []
        _FakeFetcher.instances = []
        _FakeFetcher.response = None
        _FakeFetcher.init_kwargs = {}
        self._store_patch = mock.patch.object(media_module, "Store", _FakeStore)
        self._fetcher_patch = mock.patch.object(media_module, "Fetcher", _FakeFetcher)
        self._store_patch.start()
        self._fetcher_patch.start()

    def tearDown(self) -> None:
        self._store_patch.stop()
        self._fetcher_patch.stop()

    def _run(self, concurrency: int = 4) -> dict[str, int]:
        return download_saved_images(MediaOptions(db_path=":memory:", data_dir=":memory:", concurrency=concurrency))

    def test_error_branch_records_source_page_url_not_article_url(self) -> None:
        _FakeFetcher.response = _response(503, error="http 503")
        result = self._run()
        self.assertEqual(result["errors"], 1)
        save_args = _FakeStore.instances[0].save_calls[0]
        # save_media(image, body, content_type, article_url, source_page_url, error)
        self.assertEqual(save_args[3], "https://a.test/article/1")
        self.assertEqual(save_args[4], "")

    def test_http_200_empty_body_reports_empty_body(self) -> None:
        _FakeFetcher.response = _response(200, body=b"")
        self._run()
        save_args = _FakeStore.instances[0].save_calls[0]
        self.assertEqual(save_args[5], "empty body")

    def test_blocking_store_writes_run_off_the_event_loop(self) -> None:
        _FakeFetcher.response = _response(200, body=b"bytes")
        self._run()
        store = _FakeStore.instances[0]
        self.assertTrue(store.save_calls)
        self.assertNotIn(threading.get_ident(), store.threads)

    def test_job_priority_is_computed_once_per_url(self) -> None:
        _FakeFetcher.response = _response(200, body=b"bytes")
        calls = 0
        original = media_module._job_priority

        def counting(existing):
            nonlocal calls
            calls += 1
            return original(existing)

        with mock.patch.object(media_module, "_job_priority", counting):
            self._run()
        self.assertEqual(calls, 1)

    def test_store_and_fetcher_are_closed_when_job_building_fails(self) -> None:
        def broken_init(db_path: str, data_dir: str) -> _FakeStore:
            store = _FakeStore(db_path, data_dir)
            store.jobs_error = RuntimeError("boom")
            return store

        with mock.patch.object(media_module, "Store", broken_init):
            with self.assertRaises(RuntimeError):
                self._run()
        self.assertTrue(_FakeStore.instances[0].closed)
        self.assertTrue(_FakeFetcher.instances[0].closed)

    def test_fetcher_connection_limit_matches_concurrency(self) -> None:
        _FakeFetcher.response = _response(200, body=b"bytes")
        self._run(concurrency=8)
        self.assertEqual(_FakeFetcher.init_kwargs["max_connections"], 8)
