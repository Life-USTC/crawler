from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from urllib.parse import urlsplit

import httpx

from .models import FetchResponse
from .robots import RobotsPolicy

USER_AGENT = "ustc-public-site-crawler/0.1 (+local public archive; no authentication)"


class HostRateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._last_request: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait_for = self.delay - (now - self._last_request[host])
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request[host] = time.monotonic()


class Fetcher:
    def __init__(
        self,
        *,
        delay: float = 1.0,
        timeout: float = 30.0,
        retries: int = 2,
        max_body_bytes: int = 30 * 1024 * 1024,
        user_agent: str = USER_AGENT,
        ignore_robots: bool = False,
        max_connections: int = 64,
    ) -> None:
        connection_limit = max(1, max_connections)
        limits = httpx.Limits(
            max_connections=connection_limit,
            max_keepalive_connections=min(connection_limit, 64),
        )
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
            limits=limits,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,image/*;q=0.8,*/*;q=0.1",
            },
        )
        self.rate_limiter = HostRateLimiter(delay)
        self.robots = RobotsPolicy(self.client, user_agent)
        self.retries = max(0, retries)
        self.max_body_bytes = max_body_bytes
        self.ignore_robots = ignore_robots

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResponse:
        if not self.ignore_robots and not await self.robots.allowed(url):
            return FetchResponse(
                requested_url=url,
                final_url=url,
                status=0,
                content_type="",
                headers={},
                body=b"",
                error="blocked by robots.txt",
                blocked_by_robots=True,
            )
        host = (urlsplit(url).hostname or "").lower()
        limit = max_bytes or self.max_body_bytes
        last_error = ""
        for attempt in range(self.retries + 1):
            await self.rate_limiter.wait(host)
            try:
                async with self.client.stream("GET", url) as response:
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                        retry_after = response.headers.get("retry-after")
                        try:
                            wait_for = min(float(retry_after or 0), 30.0)
                        except ValueError:
                            wait_for = 0.0
                        await asyncio.sleep(wait_for or 2**attempt)
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            return FetchResponse(
                                requested_url=url,
                                final_url=str(response.url),
                                status=response.status_code,
                                content_type=content_type,
                                headers=dict(response.headers),
                                body=b"".join(chunks),
                                error=f"body exceeds {limit} bytes",
                            )
                        chunks.append(chunk)
                    return FetchResponse(
                        requested_url=url,
                        final_url=str(response.url),
                        status=response.status_code,
                        content_type=content_type,
                        headers=dict(response.headers),
                        body=b"".join(chunks),
                    )
            except (httpx.HTTPError, UnicodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    await asyncio.sleep(2**attempt)
        return FetchResponse(
            requested_url=url,
            final_url=url,
            status=0,
            content_type="",
            headers={},
            body=b"",
            error=last_error or "request failed",
        )
