import unittest

from ustc_crawler.http import USER_AGENT, Fetcher


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


if __name__ == "__main__":
    unittest.main()
