from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .models import RobotsBlockReason

_DISALLOW_ALL = ["User-agent: *", "Disallow: /"]


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    allowed: bool
    reason: RobotsBlockReason | None = None
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class _RobotsDocument:
    parser: RobotFileParser
    http_status: int | None
    failure: RobotsBlockReason | None = None


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
        self._policies: dict[str, _RobotsDocument] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _robots_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    async def _load(self, url: str) -> _RobotsDocument:
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
            status = None
            failure = None
            try:
                response = await self.client.get(parser.url, timeout=self.timeout)
                status = response.status_code
                if status == 404:
                    parser.parse([])
                elif status >= 500:
                    parser.parse(_DISALLOW_ALL)
                    cache = False
                    failure = RobotsBlockReason.HTTP_ERROR
                elif status >= 400:
                    parser.parse(_DISALLOW_ALL)
                    failure = RobotsBlockReason.HTTP_ERROR
                else:
                    parser.parse(response.text.splitlines())
            except (httpx.DecodingError, UnicodeError):
                parser.parse(_DISALLOW_ALL)
                cache = False
                failure = RobotsBlockReason.DECODING_ERROR
            except httpx.HTTPError:
                parser.parse(_DISALLOW_ALL)
                cache = False
                failure = RobotsBlockReason.TRANSPORT_ERROR
            document = _RobotsDocument(parser, status, failure)
            if cache:
                self._policies[origin] = document
            return document

    async def decision(self, url: str) -> RobotsDecision:
        document = await self._load(url)
        allowed = document.parser.can_fetch(self.user_agent, url)
        return RobotsDecision(
            allowed=allowed,
            reason=None if allowed else document.failure or RobotsBlockReason.RULE_DISALLOWED,
            http_status=document.http_status,
        )

    async def allowed(self, url: str) -> bool:
        return (await self.decision(url)).allowed
