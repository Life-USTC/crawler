import asyncio
import unittest

import httpx
import pytest

from ustc_crawler.models import RobotsBlockReason
from ustc_crawler.robots import RobotsPolicy


def _policy(handler, calls: list[str]) -> RobotsPolicy:
    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting_handler))
    return RobotsPolicy(client, "TestAgent/1.0")


def _robots_response(status: int, body: str = "") -> httpx.Response:
    return httpx.Response(status, text=body)


class RobotsPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_404_robots_allows_everything(self) -> None:
        calls: list[str] = []
        policy = _policy(lambda request: _robots_response(404), calls)
        self.assertTrue(await policy.allowed("https://example.test/anything"))

    async def test_403_robots_disallows_everything(self) -> None:
        calls: list[str] = []
        policy = _policy(lambda request: _robots_response(403), calls)
        self.assertFalse(await policy.allowed("https://example.test/anything"))

    async def test_401_robots_disallows_everything(self) -> None:
        calls: list[str] = []
        policy = _policy(lambda request: _robots_response(401), calls)
        self.assertFalse(await policy.allowed("https://example.test/anything"))

    async def test_5xx_disallows_for_this_run_without_caching(self) -> None:
        calls: list[str] = []
        responses = [_robots_response(503), _robots_response(404)]

        def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        policy = _policy(handler, calls)
        self.assertFalse(await policy.allowed("https://example.test/anything"))
        # A server error must not be cached: the next check fetches again and
        # sees the site come back.
        self.assertTrue(await policy.allowed("https://example.test/anything"))
        self.assertEqual(len(calls), 2)

    async def test_network_error_disallows_without_caching(self) -> None:
        calls: list[str] = []
        attempts = [True, False]

        def handler(request: httpx.Request) -> httpx.Response:
            if attempts.pop(0):
                raise httpx.ConnectError("connection refused", request=request)
            return _robots_response(404)

        policy = _policy(handler, calls)
        self.assertFalse(await policy.allowed("https://example.test/anything"))
        self.assertTrue(await policy.allowed("https://example.test/anything"))
        self.assertEqual(len(calls), 2)

    async def test_parsed_rules_are_enforced_and_cached(self) -> None:
        calls: list[str] = []
        body = "User-agent: *\nDisallow: /private/\n"
        policy = _policy(lambda request: _robots_response(200, body), calls)
        self.assertFalse(await policy.allowed("https://example.test/private/page"))
        self.assertTrue(await policy.allowed("https://example.test/public/page"))
        self.assertEqual(len(calls), 1)


@pytest.mark.parametrize("status", [401, 403, 410, 429])
def test_cached_http_denial_retains_robots_status(status):
    async def run():
        calls: list[str] = []
        policy = _policy(lambda request: _robots_response(status), calls)
        try:
            for path in ["first", "second"]:
                decision = await policy.decision("https://example.test/" + path)
                assert not decision.allowed
                assert decision.reason == RobotsBlockReason.HTTP_ERROR
                assert decision.http_status == status
            assert len(calls) == 1
        finally:
            await policy.client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["http", "transport", "decoding", "unicode"])
def test_temporary_failure_reason_is_not_cached_after_recovery(failure):
    async def run():
        calls: list[str] = []

        def handler(request):
            if len(calls) == 1:
                if failure == "http":
                    return _robots_response(503)
                if failure == "transport":
                    raise httpx.ConnectError("private transport detail", request=request)
                if failure == "decoding":
                    raise httpx.DecodingError("private encoding detail", request=request)
                raise UnicodeError("private text detail")
            return _robots_response(404)

        policy = _policy(handler, calls)
        try:
            first = await policy.decision("https://example.test/first")
            assert not first.allowed
            assert first.reason == {
                "http": RobotsBlockReason.HTTP_ERROR,
                "transport": RobotsBlockReason.TRANSPORT_ERROR,
                "decoding": RobotsBlockReason.DECODING_ERROR,
                "unicode": RobotsBlockReason.DECODING_ERROR,
            }[failure]
            assert first.http_status == (503 if failure == "http" else None)
            recovered = await policy.decision("https://example.test/second")
            assert recovered.allowed
            assert recovered.reason is None
            assert recovered.http_status == 404
            assert await policy.allowed("https://example.test/third")
            assert len(calls) == 2
        finally:
            await policy.client.aclose()

    asyncio.run(run())


def test_concurrent_decisions_keep_their_own_origin_and_path_reason():
    async def run():
        calls: list[str] = []

        async def handler(request):
            calls.append(str(request.url))
            await asyncio.sleep(0)
            if request.url.host == "unavailable.test":
                return _robots_response(403)
            return _robots_response(200, "User-agent: *\nDisallow: /private/\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = RobotsPolicy(client, "TestAgent/1.0")
            private, unavailable, public = await asyncio.gather(
                policy.decision("https://rules.test/private/page"),
                policy.decision("https://unavailable.test/page"),
                policy.decision("https://rules.test/public/page"),
            )
            assert (private.allowed, private.reason, private.http_status) == (
                False, RobotsBlockReason.RULE_DISALLOWED, 200
            )
            assert (unavailable.allowed, unavailable.reason, unavailable.http_status) == (
                False, RobotsBlockReason.HTTP_ERROR, 403
            )
            assert (public.allowed, public.reason, public.http_status) == (True, None, 200)
            assert sorted(calls) == [
                "https://rules.test/robots.txt", "https://unavailable.test/robots.txt"
            ]

    asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
