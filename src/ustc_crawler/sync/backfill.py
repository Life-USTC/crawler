from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .outbox import IngestionOutbox

if TYPE_CHECKING:
    from ..store import Store


def sync_backfill(
    store: Store,
    *,
    outbox: IngestionOutbox | None = None,
    chunk_size: int = 100,
    source_ids: set[str] | None = None,
    after_url: str = "",
    limit: int = 0,
) -> dict[str, int]:
    """Keyset-enqueue existing articles without making any network requests.

    ``source_ids``, ``after_url``, and ``limit`` bound an article-keyset
    repair.  Reindexing enqueues each successfully rebuilt canonical article
    directly, so its page-keyset cursor is not reused for this operation.
    """

    if chunk_size < 1 or chunk_size > 1_000:
        raise ValueError("backfill chunk size must be between 1 and 1000")
    if limit < 0:
        raise ValueError("backfill limit must not be negative")
    outbox = outbox or IngestionOutbox(store.database)
    sources: dict[str, Any] = {}
    cursor = after_url
    remaining = limit
    result = {"scanned": 0, "enqueued": 0, "errors": 0}
    while True:
        page_limit = min(chunk_size, remaining) if remaining else chunk_size
        snapshots = store.sync_article_snapshot_page(
            cursor,
            page_limit,
            source_ids,
        )
        if not snapshots:
            break
        for snapshot in snapshots:
            article = snapshot.article
            result["scanned"] += 1
            cursor = article.url
            try:
                source = sources.get(article.source_id)
                if source is None:
                    source = store.source_descriptor(article.source_id)
                    sources[article.source_id] = source
                if source.discovery_only:
                    continue
                _event_id, created = outbox.enqueue_article_with_status(
                    article,
                    store.data_dir,
                    source=source,
                    asset_paths=snapshot.asset_paths,
                )
            except (OSError, ValueError, KeyError):
                result["errors"] += 1
                continue
            if created:
                result["enqueued"] += 1
        if remaining:
            remaining -= len(snapshots)
            if remaining <= 0:
                break
    return result


__all__ = ["sync_backfill"]
