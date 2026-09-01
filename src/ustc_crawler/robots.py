from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx


class RobotsPolicy:
    """Small robots cache; a failed robots request is treated as unavailable."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str, timeout: float = 15.0) -> None:
        self.client = client
        self.user_agent = user_agent
        self.timeout = timeout
        self._policies: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _robots_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    async def _load(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        if origin in self._policies:
            return self._policies[origin]
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._policies:
                return self._policies[origin]
            parser = RobotFileParser(self._robots_url(url))
            try:
                response = await self.client.get(parser.url, timeout=self.timeout)
                if response.status_code >= 400:
                    self._policies[origin] = None
                else:
                    parser.parse(response.text.splitlines())
                    self._policies[origin] = parser
            except (httpx.HTTPError, UnicodeError):
                self._policies[origin] = None
            return self._policies[origin]

    async def allowed(self, url: str) -> bool:
        parser = await self._load(url)
        return parser is None or parser.can_fetch(self.user_agent, url)
