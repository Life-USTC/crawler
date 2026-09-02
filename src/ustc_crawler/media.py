from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .http import Fetcher
from .models import ImageRef
from .store import Store, article_bundle_path


@dataclass(slots=True)
class MediaOptions:
    db_path: str = "data/crawler.sqlite"
    data_dir: str = "data"
    concurrency: int = 16
    delay: float = 0.5
    max_image_bytes: int = 20 * 1024 * 1024
    source_ids: tuple[str, ...] = ()


def _job_priority(existing: Any) -> int:
    """Fetch unseen media before retries, then relink files already on disk."""

    if existing is None:
        return 0
    if (
        existing["status"] == "ok"
        and existing["local_path"]
        and Path(existing["local_path"]).is_file()
    ):
        return 2
    return 1


def _interleave_jobs_by_host(
    planned: list[tuple[int, str, list[ImageRef], Any]],
) -> list[tuple[int, str, list[ImageRef], Any]]:
    """Round-robin hosts within each priority so per-host delays do not serialize a run."""

    positions: dict[tuple[int, str], int] = defaultdict(int)
    decorated: list[tuple[int, int, str, str, list[ImageRef], Any]] = []
    for priority, url, refs, existing in planned:
        host = (urlsplit(url).hostname or "").casefold()
        key = (priority, host)
        position = positions[key]
        positions[key] += 1
        decorated.append((priority, position, host, url, refs, existing))
    decorated.sort(key=lambda item: item[:4])
    return [
        (priority, url, refs, existing)
        for priority, _, _, url, refs, existing in decorated
    ]


def _image_jobs(store: Store, source_ids: set[str] | None = None) -> dict[str, list[ImageRef]]:
    """Build image jobs from structured relationships and article bundles.

    Bundles retain every extracted image even before it has been downloaded.
    Read them for every article so a partially downloaded article can acquire
    its remaining media; restricting the fallback to articles with zero
    relationships permanently skipped those missing images.
    """
    jobs: dict[str, list[ImageRef]] = {}
    seen: set[tuple[str, str]] = set()
    for row in store.article_media_records(source_ids):
        seen.add((row["image_url"], row["article_url"]))
        jobs.setdefault(row["image_url"], []).append(
            ImageRef(
                url=row["image_url"],
                alt=row["alt"] or "",
                title=row["title"] or "",
                caption=row["caption"] or "",
                article_url=row["article_url"],
            )
        )

    rows = store.article_records_for_media(source_ids)
    for row in rows:
        bundle = article_bundle_path(store.data_dir, str(row["url"]))
        if not bundle.exists():
            continue
        try:
            payload = json.loads(bundle.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        values = payload.get("images", []) if isinstance(payload, dict) else []
        for value in values:
            if not isinstance(value, dict) or not value.get("url"):
                continue
            image = ImageRef(
                url=str(value["url"]),
                alt=str(value.get("alt") or ""),
                title=str(value.get("title") or ""),
                caption=str(value.get("caption") or ""),
                article_url=row["url"],
            )
            key = (image.url, image.article_url)
            if key in seen:
                continue
            seen.add(key)
            jobs.setdefault(image.url, []).append(image)
    return jobs


async def _run(options: MediaOptions) -> dict[str, int]:
    store = Store(options.db_path, options.data_dir)
    fetcher = Fetcher(
        delay=options.delay,
        max_connections=max(64, options.concurrency),
    )
    jobs = _image_jobs(store, set(options.source_ids) or None)
    semaphore = asyncio.Semaphore(max(1, options.concurrency))
    fetched = 0
    skipped = 0
    errors = 0

    async def one(url: str, refs: list[ImageRef], existing: Any) -> None:
        nonlocal fetched, skipped, errors
        if _job_priority(existing) == 2:
            for ref in refs:
                store.link_media(ref, ref.article_url, existing["source_page_url"] or "")
            skipped += 1
            return
        async with semaphore:
            response = await fetcher.fetch(url, max_bytes=options.max_image_bytes)
        if response.status == 200 and response.body:
            store.save_media(
                refs[0],
                response.body,
                response.content_type,
                refs[0].article_url,
                existing["source_page_url"] if existing else "",
            )
            for ref in refs[1:]:
                store.link_media(ref, ref.article_url, refs[0].article_url)
            fetched += 1
            return
        error = response.error or f"http {response.status}"
        for ref in refs:
            store.save_media(
                ref,
                b"",
                response.content_type,
                ref.article_url,
                ref.article_url,
                error,
            )
        errors += 1

    try:
        planned = [
            (_job_priority(existing), url, refs, existing)
            for url, refs in jobs.items()
            for existing in (store.media_snapshot(url),)
        ]
        planned = _interleave_jobs_by_host(planned)
        await asyncio.gather(
            *(one(url, refs, existing) for _, url, refs, existing in planned)
        )
    finally:
        await fetcher.close()
        store.close()
    return {
        "unique_images": len(jobs),
        "downloaded": fetched,
        "already_local": skipped,
        "errors": errors,
    }


def download_saved_images(options: MediaOptions) -> dict[str, int]:
    return asyncio.run(_run(options))
