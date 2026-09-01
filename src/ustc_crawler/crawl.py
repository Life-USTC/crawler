from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .canonicalize import looks_like_asset, looks_like_binary, normalize_url
from .config import load_config
from .discover import discover_units, unit_sources
from .extract import extract_page
from .http import Fetcher
from .models import PageDocument, SourceConfig
from .routing import source_id_for_url
from .scoring import (
    _date_from_url,
    document_asset_url,
    is_obvious_low_value_url,
    score_page,
    url_priority,
)
from .store import Store

HTML_TYPES = {"text/html", "application/xhtml+xml", ""}


def _parse_since(value: str) -> datetime | None:
    """Normalize the --since CLI value to a timezone-aware datetime."""

    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=datetime.now().astimezone().tzinfo)
    except ValueError:
        return None


def _published_datetime(value: str) -> datetime | None:
    """Best-effort parse of an extracted publication timestamp."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _is_after_since(published_at: str, since: datetime | None) -> bool:
    """Return True if published_at is missing, invalid, or not older than since."""

    if since is None:
        return True
    published = _published_datetime(published_at)
    if published is None:
        return True
    return published.date() >= since.date()


@dataclass(slots=True)
class CrawlOptions:
    config_path: str = "config/sources.yaml"
    db_path: str = "data/crawler.sqlite"
    data_dir: str = "data"
    units_path: str = "data/discovered_units.json"
    include_units: bool = False
    include_supplemental: bool = False
    max_pages: int = 0
    max_pages_per_source: int = 0
    max_depth: int = 0
    concurrency: int = 2
    delay: float = 1.0
    download_images: bool = True
    max_images_per_page: int = 30
    max_image_bytes: int = 20 * 1024 * 1024
    ignore_robots: bool = False
    min_value_score: int = 16
    news_first: bool = True
    since: str = ""
    incremental: bool = False
    source_ids: list[str] = field(default_factory=list)


def _decode(body: bytes, headers: dict[str, str]) -> str:
    content_type = headers.get("content-type", "")
    match = re.search(r"charset\s*=\s*['\"]?([\w-]+)", content_type, re.I)
    meta_match = re.search(rb"charset\s*=\s*['\"]?([\w-]+)", body[:8192], re.I)
    encodings = [
        meta_match.group(1).decode("ascii", errors="ignore") if meta_match else "",
        "utf-8",
        "gb18030",
        match.group(1) if match else "",
        "big5",
    ]
    candidates: list[tuple[tuple[int, int, int, int], str]] = []
    for index, encoding in enumerate(dict.fromkeys(filter(None, encodings))):
        try:
            text = body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
        replacement = text.count("\ufffd")
        mojibake = text.count("Ã") + text.count("Â") + text.count("�")
        candidates.append(((chinese, -replacement, -mojibake, -index), text))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return body.decode("utf-8", errors="replace")


def _is_html(content_type: str, url: str, body: bytes) -> bool:
    if looks_like_binary(body):
        return False
    if content_type in HTML_TYPES or content_type.startswith("text/html"):
        return True
    if looks_like_asset(url):
        return False
    sample = body[:200].lstrip().lower()
    return sample.startswith(b"<!doctype html") or b"<html" in sample


def _is_xml(content_type: str, url: str) -> bool:
    path = urlsplit(url).path.lower()
    return (
        "xml" in content_type
        or path.endswith("sitemap.xml")
        or path.endswith(".rss")
        or path.endswith(".atom")
    )


def _xml_links(body: bytes, headers: dict[str, str], base_url: str) -> list[str]:
    values: list[str] = []
    try:
        # ElementTree rejects several legacy multi-byte XML declarations when
        # handed raw bytes on newer Python versions.  Decode using the same
        # charset selection used for HTML/RSS text first, then parse Unicode.
        root = ElementTree.fromstring(_decode(body, headers))
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].lower() in {"loc", "link"} and node.text:
                values.append(node.text.strip())
    except (ElementTree.ParseError, ValueError, UnicodeError):
        values = re.findall(rb"<(?:loc|link)[^>]*>(.*?)</(?:loc|link)>", body, re.I | re.S)
        values = [_decode(value, headers).strip() for value in values]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        target = normalize_url(value, base_url)
        if target and target not in seen:
            seen.add(target)
            result.append(target)
    return result


class AsyncCrawler:
    def __init__(self, options: CrawlOptions) -> None:
        self.options = options
        self.store = Store(options.db_path, options.data_dir)
        self.configured_sources: dict[str, SourceConfig] = {}
        self.sources: dict[str, SourceConfig] = {}
        self.fetcher = Fetcher(delay=options.delay, ignore_robots=options.ignore_robots)
        self.queue: asyncio.PriorityQueue[tuple[int, int, str, str, int, str]] = asyncio.PriorityQueue()
        self.enqueued: set[str] = set()
        self._queue_sequence = 0
        self.source_counts: dict[str, int] = {}
        self.processed = 0
        self.articles = 0
        self.media = 0
        self.errors = 0
        self._counter_lock = asyncio.Lock()
        self.source_since: dict[str, datetime] = {}
        self.sync_run_id = uuid.uuid4().hex
        self.sync_run_started = False

    async def close(self) -> None:
        await self.fetcher.close()
        self.store.close()

    def prepare(self) -> None:
        sources, directory_url = load_config(
            self.options.config_path, include_supplemental=self.options.include_supplemental
        )
        if self.options.include_units:
            units_file = Path(self.options.units_path)
            if not units_file.exists():
                discover_units(directory_url, units_file)
            sources.extend(unit_sources(units_file))
        eligible_sources = {
            source.id: source
            for source in sources
            if not source.discovery_only or self.options.include_supplemental
        }
        selected = set(self.options.source_ids) if self.options.source_ids else set()
        self.configured_sources = eligible_sources
        self.sources = {
            source_id: source
            for source_id, source in eligible_sources.items()
            if not selected or source_id in selected
        }
        source_config_revision = hashlib.sha256(
            json.dumps(
                [
                    {
                        "id": source.id,
                        "name": source.name,
                        "organization_level": source.organization_level,
                        "allowed_hosts": sorted(source.allowed_hosts),
                        "blocked_hosts": sorted(source.blocked_hosts),
                        "seed_urls": sorted(source.seed_urls),
                        "aliases": sorted(source.aliases),
                        "discovery_only": source.discovery_only,
                        "max_images_per_page": source.max_images_per_page,
                    }
                    for source in sorted(self.configured_sources.values(), key=lambda item: item.id)
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.store.start_sync_run(
            self.sync_run_id,
            mode="incremental" if self.options.incremental else "full",
            source_config_revision=source_config_revision,
            digest=source_config_revision,
        )
        self.sync_run_started = True
        self.store.add_sources(self.configured_sources.values())
        if self.options.incremental:
            self.source_since = self.store.source_newest_dates(set(self.sources.keys()))
            source_seeds = {
                source.id: {normalize_url(seed) for seed in source.seed_urls if seed}
                for source in self.sources.values()
            }
            self.store.reset_seeds_and_listings(source_seeds)
        self.store.reset_processing()
        self.store.filter_frontier(self.options.min_value_score)
        for source in self.sources.values():
            for seed in source.seed_urls:
                if seed:
                    # A newly added source must get one fair turn before the
                    # legacy frontier's thousands of article URLs. Its own
                    # links will then receive normal news/resource priority.
                    seed_priority = max(500, url_priority(seed, source.id))
                    self.store.enqueue(seed, source.id, 0, "", seed_priority)
                    parts = urlsplit(seed)
                    for sitemap_path in ("/sitemap.xml", "/wp-sitemap.xml"):
                        sitemap = f"{parts.scheme}://{parts.netloc}{sitemap_path}"
                        self.store.enqueue(sitemap, source.id, 0, seed, seed_priority + 80)
        for row in self.store.pending():
            if row["source_id"] in self.sources:
                self.enqueued.add(row["url"])

    def _source_for_url(self, url: str) -> SourceConfig | None:
        """Return the configured source that owns a discovered URL."""

        candidates = {
            **self.configured_sources,
            **self.sources,
        }
        owner_id = source_id_for_url(
            url,
            {
                source_id: (source.allowed_hosts, source.blocked_hosts)
                for source_id, source in candidates.items()
            },
        )
        return candidates.get(owner_id)

    def _before_cutoff(
        self,
        url: str,
        source_id: str,
        depth: int,
        published_at: str,
    ) -> tuple[bool, str]:
        """Decide whether a dated URL should be excluded from this run."""

        explicit_since = _parse_since(self.options.since)
        if explicit_since and not _is_after_since(published_at, explicit_since):
            return True, f"published {published_at} is before --since {self.options.since}"
        incremental_since = self.source_since.get(source_id)
        if incremental_since and not _is_after_since(published_at, incremental_since):
            # A current shallow listing can expose a gap older than the newest
            # stored article. Fetch that unseen item, while still refusing to
            # replay known articles and deep historical archives.
            if depth > 4 or self.store.article_exists(url) or self.store.article_alias_exists(url):
                return True, f"published {published_at} is before incremental cutoff"
        return False, ""

    async def _enqueue(
        self,
        url: str,
        source: SourceConfig,
        depth: int,
        parent: str,
        published_at: str = "",
    ) -> None:
        url = normalize_url(url)
        source = self._source_for_url(url) if url else None
        if source is None:
            return
        is_sitemap = urlsplit(url).path.lower().endswith("sitemap.xml")
        is_document = document_asset_url(url)
        low_value, _ = is_obvious_low_value_url(url)
        if (
            not url
            or (looks_like_asset(url) and not is_sitemap and not is_document)
            or low_value
        ):
            return
        if self.options.max_depth and depth > self.options.max_depth:
            return
        if url in self.enqueued:
            return
        # A date encoded in a USTC article URL is more reliable than a hint
        # parsed from surrounding listing text, which may belong to a nearby
        # link or banner.
        published_at = _date_from_url(url) or published_at or self.store.article_hint(url)
        if self.options.incremental and not published_at and depth > 4:
            self.store.mark_filtered(url, "incremental skip: undated deep link")
            return
        before_cutoff, cutoff_reason = self._before_cutoff(
            url, source.id, depth, published_at
        )
        if before_cutoff:
            self.store.mark_filtered(url, cutoff_reason)
            return
        # In incremental mode, avoid re-crawling old document attachments that
        # were discovered from listing pages.  PDF/Word/etc. notices without a
        # date hint are assumed to be legacy attachments; new attachments are
        # still downloaded when linked from freshly published articles.
        if self.options.incremental and is_document and not published_at:
            self.store.mark_filtered(url, "incremental skip: document without date hint")
            return
        priority = (
            url_priority(url, source.id, parent, published_at)
            if self.options.news_first
            else 0
        )
        revive_current = bool(
            self.source_since.get(source.id)
            and depth <= 4
            and self.store.needs_current_article_repair(url)
        )
        if self.store.enqueue(
            url,
            source.id,
            depth,
            parent,
            priority,
            revive_current=revive_current,
        ):
            self.enqueued.add(url)
            if source.id in self.sources:
                self._queue_sequence += 1
                await self.queue.put(
                    (-priority, self._queue_sequence, url, source.id, depth, parent)
                )

    async def _queue_existing(self, url: str, source_id: str, depth: int, parent: str, priority: int) -> None:
        self._queue_sequence += 1
        await self.queue.put((-priority, self._queue_sequence, url, source_id, depth, parent))

    async def _seed_queue(self) -> None:
        rows = self.store.pending()
        # The persisted frontier is already ordered by the explicit priority:
        # publication/news URLs, then course resources, then ordinary pages.
        # This is intentionally global so a
        # large generic site cannot postpone news on every other official host.
        for row in rows:
            if (
                row["source_id"] not in self.configured_sources
                and row["source_id"] not in self.sources
            ):
                # Unit and supplemental catalogs are optional inputs. A run
                # that did not load one of those catalogs must not invalidate
                # its persisted handoffs.
                continue
            owner = self._source_for_url(row["url"])
            if owner is None:
                self.store.mark_filtered(row["url"], "host has no configured source owner")
                continue
            source_id = owner.id
            if source_id != row["source_id"]:
                self.store.set_frontier_source(row["url"], source_id)
            if source_id not in self.sources:
                continue
            if self.options.max_depth and int(row["depth"] or 0) > self.options.max_depth:
                self.store.mark_filtered(
                    row["url"],
                    f"depth {row['depth']} exceeds max depth {self.options.max_depth}",
                )
                continue
            if not normalize_url(row["url"]):
                self.store.mark_filtered(row["url"], "malformed URL placeholder")
                continue
            low_value, reason = is_obvious_low_value_url(row["url"])
            if low_value:
                self.store.mark_filtered(row["url"], reason)
                continue
            saved = self.store.page_snapshot(row["url"])
            if (
                self.options.incremental
                and int(row["depth"] or 0) > 0
                and saved
                and str(saved["page_kind"] or "unknown")
                not in {"news_listing", "feed", "news_article", "article"}
            ):
                self.store.mark_filtered(
                    row["url"],
                    f"incremental skip: existing {saved['page_kind']} page",
                )
                continue
            if row["depth"] > 0 and saved and int(saved["value_score"] or 0) < self.options.min_value_score:
                self.store.mark_filtered(
                    row["url"],
                    f"previous score {saved['value_score']} below crawl threshold ({saved['page_kind']})",
                )
                continue
            published_at = _date_from_url(row["url"]) or self.store.article_hint(row["url"])
            if (
                self.options.incremental
                and not published_at
                and int(row["depth"] or 0) > 4
            ):
                self.store.mark_filtered(row["url"], "incremental skip: undated deep link")
                continue
            before_cutoff, cutoff_reason = self._before_cutoff(
                row["url"], source_id, int(row["depth"] or 0), published_at
            )
            if before_cutoff:
                self.store.mark_filtered(row["url"], cutoff_reason)
                continue
            priority = int(row["priority"] or 0)
            if self.options.news_first:
                priority = url_priority(row["url"], source_id, row["discovered_from"] or "", published_at)
                # Seed URLs are given a fair turn before the legacy backlog.
                if row["depth"] == 0 and not row["discovered_from"]:
                    priority = max(500, priority)
                self.store.set_frontier_priority(row["url"], priority)
            await self._queue_existing(
                row["url"],
                source_id,
                row["depth"],
                row["discovered_from"] or "",
                priority,
            )

    async def _download_images(self, article, source_page_url: str) -> None:
        if not self.options.download_images:
            return
        source = self.sources.get(article.source_id)
        per_source_cap = source.max_images_per_page if source and source.max_images_per_page is not None else self.options.max_images_per_page
        cap = per_source_cap if per_source_cap > 0 else 0
        images = article.images if cap <= 0 else article.images[:cap]
        for image in images:
            response = await self.fetcher.fetch(image.url, max_bytes=self.options.max_image_bytes)
            if response.status == 200 and response.body:
                self.store.save_media(
                    image, response.body, response.content_type, article.url, source_page_url
                )
                self.media += 1
            else:
                self.store.save_media(
                    image,
                    b"",
                    response.content_type,
                    article.url,
                    source_page_url,
                    response.error or f"http {response.status}",
                )
                self.errors += 1

    async def _process(self, url: str, source_id: str, depth: int, parent: str) -> None:
        source = self.sources[source_id]
        async with self._counter_lock:
            if self.options.max_pages and self.processed >= self.options.max_pages:
                return
            if (
                self.options.max_pages_per_source
                and self.source_counts.get(source_id, 0) >= self.options.max_pages_per_source
            ):
                return
            self.processed += 1
            self.source_counts[source_id] = self.source_counts.get(source_id, 0) + 1
        self.store.mark_processing(url)
        response = await self.fetcher.fetch(url)
        if (
            response.blocked_by_robots
            or response.error
            or response.status >= 400
            or response.status == 0
        ):
            page = PageDocument(
                requested_url=url,
                final_url=response.final_url,
                status=response.status,
                content_type=response.content_type,
                fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                title="",
                canonical_url=url,
                html="",
                links=[],
                images=[],
                error=response.error or f"http {response.status}",
                blocked_by_robots=response.blocked_by_robots,
            )
            result = score_page(
                url=url,
                final_url=response.final_url,
                status=response.status,
                content_type=response.content_type,
                blocked_by_robots=response.blocked_by_robots,
            )
            page.page_kind = result.page_kind
            page.access_mode = result.access_mode
            page.value_score = result.value_score
            page.value_tier = result.value_tier
            page.score_reasons = result.score_reasons
            page.published_at = result.published_at
            self.store.save_page(page, source_id, depth, parent)
            self.store.mark_done(url, page.error)
            self.store.failure(url, source_id, page.error, response.status)
            self.errors += 1
            return
        if _is_xml(response.content_type, response.final_url):
            xml_text = _decode(response.body, response.headers)
            page = PageDocument(
                requested_url=url,
                final_url=response.final_url,
                status=response.status,
                content_type=response.content_type,
                fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                title="sitemap/feed",
                canonical_url=response.final_url,
                html=xml_text,
                links=_xml_links(response.body, response.headers, response.final_url),
                images=[],
                raw_body=response.body,
            )
            page.page_kind = "sitemap" if "sitemap" in url.lower() else "feed"
            page.access_mode = "public"
            page.value_score = 18
            page.value_tier = "audit_only"
            page.score_reasons = ["discovery feed; useful for URL coverage, not article index"]
            self.store.save_page(page, source_id, depth, parent)
            self.store.save_links(url, source_id, [(target, "page") for target in page.links])
            for target in page.links:
                await self._enqueue(target, source, depth + 1, url)
            self.store.mark_done(url)
            return
        if not _is_html(response.content_type, response.final_url, response.body):
            if response.status == 200 and document_asset_url(response.final_url or url):
                asset_result = score_page(
                    url=url,
                    final_url=response.final_url,
                    title=response.final_url.rsplit("/", 1)[-1],
                    body_text="",
                    status=response.status,
                    content_type=response.content_type,
                    document_link_count=1,
                )
                document_page = PageDocument(
                    requested_url=url,
                    final_url=response.final_url,
                    status=response.status,
                    content_type=response.content_type,
                    fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    title=response.final_url.rsplit("/", 1)[-1],
                    canonical_url=response.final_url,
                    html="",
                    links=[],
                    images=[],
                    page_kind=asset_result.page_kind,
                    access_mode=asset_result.access_mode,
                    value_score=asset_result.value_score,
                    value_tier=asset_result.value_tier,
                    score_reasons=asset_result.score_reasons,
                    published_at=asset_result.published_at,
                )
                self.store.save_page(document_page, source_id, depth, parent)
                self.store.save_asset(
                    url=url,
                    source_url=parent,
                    body=response.body,
                    mime_type=response.content_type,
                    access_mode=asset_result.access_mode,
                    value_score=asset_result.value_score,
                    score_reasons=asset_result.score_reasons,
                )
                self.store.mark_done(url)
                return
            self.store.mark_done(url)
            return
        html = _decode(response.body, response.headers)
        page = extract_page(response.final_url, html, response.content_type, source_id)
        page.requested_url = url
        page.final_url = response.final_url
        page.status = response.status
        page.fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
        page.raw_body = response.body
        if page.article:
            # The page source records where the response was fetched, while
            # the article belongs to the configured source that owns its
            # canonical URL.  A discovery page can therefore publish an
            # article whose canonical URL is hosted by another USTC source;
            # preserve the fetched page's source_id and route the article,
            # media, and sync event through the canonical owner.
            owner = self._source_for_url(page.article.url)
            if owner is None:
                # An external canonical URL is not a publication owned by
                # this crawler.  Keep the raw fetched page for audit, but do
                # not turn an escaped canonical page into a searchable or
                # publishable article.
                page.article = None
            else:
                page.article.source_id = owner.id
        requested_published_at = _date_from_url(url)
        if page.article and requested_published_at:
            page.article.published_at = requested_published_at
        document_link_count = sum(1 for target in page.links if document_asset_url(target))
        duplicate_of = self.store.duplicate_page_url(
            hashlib.sha256(response.body).hexdigest(),
            url,
            prefer_article=page.article is not None,
        )
        result = score_page(
            url=url,
            final_url=response.final_url,
            title=page.title,
            body_text=page.article.body_text if page.article else "",
            html=html,
            status=response.status,
            content_type=response.content_type,
            has_article=page.article is not None,
            published_at=page.article.published_at if page.article else "",
            link_count=len(page.links),
            document_link_count=document_link_count,
            duplicate=bool(duplicate_of),
        )
        page.page_kind = result.page_kind
        page.access_mode = result.access_mode
        page.value_score = result.value_score
        page.value_tier = result.value_tier
        page.score_reasons = result.score_reasons
        page.published_at = result.published_at
        page.duplicate_of = duplicate_of
        article = page.article
        if article and (result.value_score < self.options.min_value_score or duplicate_of):
            # Keep the raw HTML and score, but do not leave a low-value or
            # duplicate article in the searchable article table.
            page.article = None
        suppressed_by_cutoff = False
        refresh_existing_before_cutoff = False
        explicit_since = _parse_since(self.options.since)
        if article and explicit_since and not _is_after_since(
            article.published_at, explicit_since
        ):
            # Keep page.article attached so save_page does not interpret a
            # caller-selected cutoff as the article disappearing from the site.
            article = None
            suppressed_by_cutoff = True
        incremental_since = self.source_since.get(source_id)
        if (
            article
            and incremental_since
            and not _is_after_since(article.published_at, incremental_since)
            and (self.store.article_exists(article.url) or self.store.article_exists(url))
        ):
            # The network page was fetched and parsed already. Refresh the
            # existing record so its fields and bundle remain an exact view of
            # the archived raw HTML, but do not redownload historical media.
            refresh_existing_before_cutoff = True
        self.store.save_page(page, source_id, depth, parent)
        links = [(target, "asset" if looks_like_asset(target) else "page") for target in page.links]
        self.store.save_links(url, source_id, links)
        for target, published_at in page.link_dates.items():
            self.store.save_article_hint(target, published_at, url)
        if article and result.value_score >= self.options.min_value_score and not duplicate_of:
            hint = self.store.article_hint(article.url) or self.store.article_hint(url)
            if hint and not article.published_at:
                article.published_at = hint
            self.store.save_article(article)
            self.articles += 1
            if not refresh_existing_before_cutoff:
                await self._download_images(article, response.final_url)
            if not source.discovery_only:
                self.store.save_article_and_enqueue_for_sync(
                    article,
                    run_id=self.sync_run_id if self.sync_run_started else None,
                )
        should_follow = (
            depth == 0
            or result.value_score >= self.options.min_value_score
            or result.page_kind in {"news_listing", "course_resource"}
            or url_priority(url, source_id, parent) >= 400
        )
        if (self.options.incremental and page.article is not None) or suppressed_by_cutoff:
            # Incremental discovery starts from refreshed listings. Article
            # pages contain archive navigation and related-content links that
            # otherwise expand back through the full historical site.
            should_follow = False
        if should_follow and not duplicate_of:
            for target in page.links:
                await self._enqueue(
                    target, source, depth + 1, url, page.link_dates.get(target, "")
                )
        self.store.mark_done(url)

    async def _worker(self) -> None:
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except TimeoutError:
                return
            try:
                await self._process(item[2], item[3], item[4], item[5])
            except Exception as exc:  # keep one malformed page from stopping a whole host
                self.errors += 1
                self.store.mark_done(item[2], f"{type(exc).__name__}: {exc}")
                self.store.failure(item[2], item[3], f"{type(exc).__name__}: {exc}")
            finally:
                self.queue.task_done()

    async def run(self) -> dict[str, int]:
        self.prepare()
        try:
            await self._seed_queue()
            workers = [
                asyncio.create_task(self._worker()) for _ in range(max(1, self.options.concurrency))
            ]
            await self.queue.join()
            await asyncio.gather(*workers)
            result = {
                "processed": self.processed,
                "articles": self.articles,
                "media": self.media,
                "errors": self.errors,
                **self.store.stats(),
            }
            self.store.finish_sync_run(
                self.sync_run_id,
                status="completed",
                pages=self.processed,
                articles=self.articles,
                media=self.media,
                errors=self.errors,
            )
            return result
        except Exception as exc:
            self.store.finish_sync_run(
                self.sync_run_id,
                status="failed",
                pages=self.processed,
                articles=self.articles,
                media=self.media,
                errors=self.errors + 1,
                last_error=f"{type(exc).__name__}: {exc}",
            )
            raise


def run_crawl(options: CrawlOptions) -> dict[str, int]:
    crawler = AsyncCrawler(options)

    async def runner() -> dict[str, int]:
        try:
            return await crawler.run()
        finally:
            await crawler.close()

    return asyncio.run(runner())
