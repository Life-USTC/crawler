import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest import mock

import httpx

from ustc_crawler.http import USER_AGENT, Fetcher


def _fetcher(handler, **kwargs) -> Fetcher:
    kwargs.setdefault("delay", 0)
    return Fetcher(transport=httpx.MockTransport(handler), **kwargs)


class HttpClientTests(unittest.TestCase):
    def test_default_user_agent_looks_like_a_browser(self) -> None:
        self.assertIn("Mozilla/5.0", USER_AGENT)
        self.assertIn("Chrome/", USER_AGENT)

    def test_fetcher_sends_browser_headers(self) -> None:
        fetcher = Fetcher()
        try:
            headers = fetcher.client.headers
            self.assertIn("Mozilla/5.0", headers["User-Agent"])
            self.assertIn("zh-CN", headers["Accept-Language"])
        finally:
            import asyncio
            asyncio.run(fetcher.close())


class FetchBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_on_429_then_succeeds(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, text="ok")

        fetcher = _fetcher(handler, retries=2)
        try:
            with mock.patch("ustc_crawler.http.asyncio.sleep") as sleep:
                async def instant(_seconds):
                    return None

                sleep.side_effect = instant
                response = await fetcher.fetch("https://example.test/page")
        finally:
            await fetcher.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"ok")
        self.assertEqual(calls, 2)

    async def test_retry_after_http_date_is_parsed(self) -> None:
        future = format_datetime(datetime.now(UTC) + timedelta(seconds=20))
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            calls += 1
            if calls == 1:
                return httpx.Response(503, headers={"Retry-After": future})
            return httpx.Response(200, text="ok")

        waits: list[float] = []
        fetcher = _fetcher(handler, retries=1)
        try:
            with mock.patch("ustc_crawler.http.asyncio.sleep") as sleep:
                async def record(seconds):
                    waits.append(seconds)

                sleep.side_effect = record
                response = await fetcher.fetch("https://example.test/page")
        finally:
            await fetcher.close()

        self.assertEqual(response.status, 200)
        # The HTTP-date delay must be honored (clamped to 30s), not treated as
        # an unparseable header that falls back to the 2**0 = 1s backoff.
        self.assertTrue(waits)
        self.assertGreater(waits[0], 2.0)
        self.assertLessEqual(waits[0], 30.0)

    async def test_max_bytes_truncates_body_with_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, content=b"x" * 100)

        fetcher = _fetcher(handler)
        try:
            response = await fetcher.fetch("https://example.test/big", max_bytes=10)
        finally:
            await fetcher.close()

        self.assertEqual(response.status, 200)
        self.assertIn("body exceeds", response.error)

    async def test_robots_disallow_blocks_fetch(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
            return httpx.Response(200, text="ok")

        fetcher = _fetcher(handler)
        try:
            blocked = await fetcher.fetch("https://example.test/private/page")
            allowed = await fetcher.fetch("https://example.test/public/page")
        finally:
            await fetcher.close()

        self.assertTrue(blocked.blocked_by_robots)
        self.assertEqual(blocked.status, 0)
        self.assertEqual(allowed.status, 200)

    async def test_transport_error_converges_to_status_zero(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            raise httpx.ConnectError("connection refused", request=request)

        fetcher = _fetcher(handler, retries=2)
        try:
            with mock.patch("ustc_crawler.http.asyncio.sleep") as sleep:
                async def instant(_seconds):
                    return None

                sleep.side_effect = instant
                response = await fetcher.fetch("https://example.test/page")
        finally:
            await fetcher.close()

        self.assertEqual(response.status, 0)
        self.assertIn("ConnectError", response.error)


if __name__ == "__main__":
    unittest.main()
