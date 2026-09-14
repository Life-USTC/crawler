import unittest

import httpx

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


if __name__ == "__main__":
    unittest.main()
