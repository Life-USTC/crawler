from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

_DISALLOW_ALL = ["User-agent: *", "Disallow: /"]


class RobotsPolicy:
    """Small robots cache with RFC 9309 fetch-failure semantics.

    A 404 means the site has no robots policy and everything is allowed.
    401/403 (and other 4xx) mean the site is unavailable to us, so everything
    is disallowed.  5xx responses and network errors are temporary: requests
    are blocked for this run, but the failure is not cached so a later check
    fetches robots.txt again.
    """

    def __init__(self, client: httpx.AsyncClient, user_agent: str, timeout: float = 15.0) -> None:
        self.client = client
        self.user_agent = user_agent
        self.timeout = timeout
        self._policies: dict[str, RobotFileParser] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _robots_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    async def _load(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        if origin in self._policies:
            return self._policies[origin]
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._policies:
                return self._policies[origin]
            parser = RobotFileParser(self._robots_url(url))
            cache = True
            try:
                response = await self.client.get(parser.url, timeout=self.timeout)
                status = response.status_code
                if status == 404:
                    parser.parse([])
                elif status >= 500:
                    parser.parse(_DISALLOW_ALL)
                    cache = False
                elif status >= 400:
                    parser.parse(_DISALLOW_ALL)
                else:
                    parser.parse(response.text.splitlines())
            except (httpx.HTTPError, UnicodeError):
                parser.parse(_DISALLOW_ALL)
                cache = False
            if cache:
                self._policies[origin] = parser
            return parser

    async def allowed(self, url: str) -> bool:
        parser = await self._load(url)
        return parser.can_fetch(self.user_agent, url)
