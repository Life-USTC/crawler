from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .canonicalize import host_matches, looks_like_asset, looks_like_binary, normalize_url
from .db import ALEMBIC_HEAD, Database, upgrade_database
from .db.core import CoreConnection, RowMapping
from .db.models import Article, ArticleMedia, Asset, Frontier, Media, Page, Source, SyncRun
from .models import (
    ArticleDocument,
    ImageRef,
    PageDocument,
    SourceConfig,
    sanitize_article_document,
    sanitize_json_value,
)
from .publication import CLASSIFIER_VERSION, classify_publication
from .scoring import url_priority
from .sync.models import build_publication
from .sync.outbox import IngestionOutbox, spool_article_objects, wire_manifest


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def article_bundle_key(url: str) -> str:
    """Return the stable archive identity for one article URL."""
    return sha256_bytes(url.encode("utf-8", errors="replace"))


def article_bundle_path(data_dir: str | Path, url: str, suffix: str = ".json") -> Path:
    return Path(data_dir) / "articles" / f"{article_bundle_key(url)}{suffix}"


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class ArticleSyncSnapshot:
    """An article and its local objects loaded for one sync-backfill chunk."""

    article: ArticleDocument
    media_paths: dict[str, tuple[str, str]]
    asset_paths: dict[str, tuple[str, str]]


class Store:
    def __init__(self, db_path: str | Path, data_dir: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir) if data_dir else self.db_path.parent
        for child in ("pages", "media", "articles", "assets", "exports"):
            (self.data_dir / child).mkdir(parents=True, exist_ok=True)
        # A missing/empty file is safe to initialize.  Existing databases must
        # be upgraded explicitly with ``ustc-crawler db-upgrade`` so opening a
        # read/report command can never mutate a user's database unexpectedly.
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            upgrade_database(self.db_path)
        self.database = Database(self.db_path, create_schema=False)
        self.database.assert_schema_head(ALEMBIC_HEAD)
        self._core = CoreConnection(self.database.engine)

    def close(self) -> None:
        self._core.close()
        self.database.close()

    def add_source(self, source: SourceConfig) -> None:
        self._core.execute(
            """INSERT INTO sources(id,name,organization_level,allowed_hosts,blocked_hosts,seed_urls,aliases,discovery_only,max_images_per_page,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                 organization_level=excluded.organization_level, allowed_hosts=excluded.allowed_hosts,
                 blocked_hosts=excluded.blocked_hosts,
                 seed_urls=excluded.seed_urls, aliases=excluded.aliases, discovery_only=excluded.discovery_only,
                 max_images_per_page=excluded.max_images_per_page""",
            (
                source.id,
                source.name,
                source.organization_level,
                json.dumps(source.allowed_hosts, ensure_ascii=False),
                json.dumps(source.blocked_hosts, ensure_ascii=False),
                json.dumps(source.seed_urls, ensure_ascii=False),
                json.dumps(source.aliases, ensure_ascii=False),
                int(source.discovery_only),
                source.max_images_per_page,
                utc_now(),
            ),
        )
        self._core.commit()

    def add_sources(self, sources: Iterable[SourceConfig]) -> None:
        for source in sources:
            self.add_source(source)

    def source_descriptor(self, source_id: str):
        """Return the persisted source snapshot used by ingestion batches."""

        from .sync.models import PublicationSourceDescriptor

        with self.database.session_factory() as session:
            row = session.get(Source, source_id)
            if row is None:
                raise KeyError(source_id)
            return PublicationSourceDescriptor(
                id=row.id,
                name=row.name,
                organizationLevel=row.organization_level,
                allowedHosts=json.loads(row.allowed_hosts),
                blockedHosts=json.loads(row.blocked_hosts),
                seedUrls=json.loads(row.seed_urls),
                aliases=json.loads(row.aliases),
                discoveryOnly=bool(row.discovery_only),
                maxImagesPerPage=row.max_images_per_page,
            )

    def enqueue(
        self,
        url: str,
        source_id: str,
        depth: int = 0,
        discovered_from: str = "",
        priority: int = 0,
        *,
        revive_current: bool = False,
    ) -> bool:
        cursor = self._core.execute(
            """INSERT OR IGNORE INTO frontier(url,source_id,depth,discovered_from,status,discovered_at,priority)
               VALUES(?,?,?,?, 'pending', ?, ?)""",
            (url, source_id, depth, discovered_from, utc_now(), priority),
        )
        inserted = cursor.rowcount > 0
        if not inserted and revive_current:
            revived = self._core.execute(
                """UPDATE frontier SET source_id=?,depth=?,discovered_from=?,status='pending',
                   last_error='',priority=MAX(priority, ?)
                   WHERE url=? AND (
                       status='done'
                       OR (status='filtered' AND last_error LIKE '%incremental cutoff%')
                   )""",
                (source_id, depth, discovered_from, priority, url),
            )
            inserted = revived.rowcount > 0
        if not inserted:
            # A URL can first be discovered from a generic page and later from
            # a news/assignment page.  Keep the stronger priority while it is
            # still pending, without resetting a completed request.
            self._core.execute(
                "UPDATE frontier SET priority=MAX(priority, ?) WHERE url=? AND status='pending'",
                (priority, url),
            )
        self._core.commit()
        return inserted

    def enqueue_many(self, urls: Iterable[tuple[str, str, int, str]]) -> int:
        count = 0
        for url, source_id, depth, parent in urls:
            if self.enqueue(url, source_id, depth, parent):
                count += 1
        return count

    def set_frontier_source(self, url: str, source_id: str) -> None:
        """Update persisted URL ownership through the typed ORM layer."""

        with self.database.session_factory.begin() as session:
            row = session.get(Frontier, url)
            if row is not None:
                row.source_id = source_id

    def set_frontier_priority(self, url: str, priority: int) -> None:
        with self.database.session_factory.begin() as session:
            row = session.get(Frontier, url)
            if row is not None:
                row.priority = priority

    def page_snapshot(self, url: str) -> dict[str, Any] | None:
        """Return the fields needed by crawl scheduling without raw SQL access."""

        with self.database.session_factory() as session:
            row = session.get(Page, url)
            if row is None:
                return None
            return {
                "value_score": row.value_score,
                "page_kind": row.page_kind,
                "access_mode": row.access_mode,
            }

    def article_media_records(self, source_ids: set[str] | None = None) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            query = (
                select(
                    ArticleMedia.article_url,
                    ArticleMedia.image_url,
                    ArticleMedia.alt,
                    ArticleMedia.title,
                    ArticleMedia.caption,
                )
                .join(Article, Article.url == ArticleMedia.article_url)
                .order_by(ArticleMedia.article_url, ArticleMedia.image_url)
            )
            if source_ids:
                query = query.where(Article.source_id.in_(source_ids))
            rows = session.execute(query).all()
            return [
                {
                    "article_url": row.article_url,
                    "image_url": row.image_url,
                    "alt": row.alt or "",
                    "title": row.title or "",
                    "caption": row.caption or "",
                }
                for row in rows
            ]

    def article_records_for_media(self, source_ids: set[str] | None = None) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            query = select(Article.url, Article.content_hash).order_by(Article.url)
            if source_ids:
                query = query.where(Article.source_id.in_(source_ids))
            rows = session.execute(query).all()
            return [{"url": row.url, "content_hash": row.content_hash or ""} for row in rows]

    def media_snapshot(self, url: str) -> dict[str, Any] | None:
        with self.database.session_factory() as session:
            row = session.get(Media, url)
            if row is None:
                return None
            return {
                "url": row.url,
                "article_url": row.article_url or "",
                "source_page_url": row.source_page_url or "",
                "local_path": row.local_path or "",
                "status": row.status,
            }

    def start_sync_run(
        self,
        run_id: str,
        *,
        mode: str,
        source_config_revision: str = "",
        digest: str | None = None,
        started_at: str | None = None,
    ) -> None:
        now = started_at or utc_now()
        with self.database.session_factory.begin() as session:
            session.add(
                SyncRun(
                    id=run_id,
                    started_at=now,
                    mode=mode,
                    source_config_revision=source_config_revision,
                    digest=digest,
                    status="running",
                )
            )

    def finish_sync_run(
        self,
        run_id: str,
        *,
        status: str,
        pages: int = 0,
        articles: int = 0,
        media: int = 0,
        errors: int = 0,
        last_error: str = "",
    ) -> None:
        with self.database.session_factory.begin() as session:
            run = session.get(SyncRun, run_id)
            if run is None:
                raise KeyError(run_id)
            run.finished_at = utc_now()
            run.status = status
            run.pages = pages
            run.articles = articles
            run.media = media
            run.errors = errors
            run.last_error = last_error or None

    def reset_processing(self) -> None:
        self._core.execute("UPDATE frontier SET status='pending' WHERE status='processing'")
        self._core.commit()

    def reset_seeds_and_listings(self, source_seeds: dict[str, set[str]]) -> int:
        """Re-enqueue seed URLs and news/course listing pages for incremental recrawl.

        Listing pages are the main source of newly published article links.  In
        incremental mode we revisit the shallow channel pages where new links
        appear, but do not replay every page of a deep historical archive.
        Article pages older than the source cutoff are still filtered during
        enqueue/process.
        """
        if not source_seeds:
            return 0
        source_ids = set(source_seeds)
        placeholders = ",".join("?" for _ in source_ids)
        params = tuple(sorted(source_ids))
        # A previously interrupted incremental run may have left thousands of
        # already-fetched archive pagination pages pending. Restore those deep
        # listings before resetting the current channel pages.
        self._core.execute(
            f"""UPDATE frontier SET status='done', last_error=NULL
                WHERE source_id IN ({placeholders})
                  AND status='pending'
                  AND depth > 3
                  AND url IN (
                      SELECT url FROM pages
                      WHERE source_id IN ({placeholders})
                        AND page_kind IN ('news_listing','course_resource','sitemap','feed')
                  )""",
            params + params,
        )
        # Reset shallow listing/channel pages. Depth three covers a seed ->
        # section -> listing -> first pagination-page traversal.
        self._core.execute(
            f"""UPDATE frontier SET status='pending'
                WHERE source_id IN ({placeholders})
                  AND status IN ('done','error','filtered')
                  AND url IN (
                      SELECT url FROM pages
                      WHERE source_id IN ({placeholders})
                        AND page_kind IN ('news_listing','course_resource','sitemap','feed')
                        AND depth <= 3
                  )""",
            params + params,
        )
        # Only currently configured seeds are reset. Historical depth-zero
        # seeds remain in the audit trail without retrying a permanently stale
        # endpoint on every incremental run.
        configured_seeds = [
            (source_id, seed)
            for source_id, seeds in sorted(source_seeds.items())
            for seed in sorted(seeds)
        ]
        if configured_seeds:
            self._core.executemany(
                """UPDATE frontier SET status='pending'
                   WHERE source_id=? AND url=?
                     AND status IN ('done','error','filtered')""",
                configured_seeds,
            )
        self._core.commit()
        return self._core.total_changes

    def pending(self, source_id: str | None = None, limit: int = 0) -> list[RowMapping]:
        clauses = ["status='pending'"]
        params: list[Any] = []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        limit_sql = " LIMIT ?" if limit else ""
        if limit:
            params.append(limit)
        return self._core.execute(
            f"SELECT * FROM frontier WHERE {' AND '.join(clauses)} ORDER BY priority DESC, depth, discovered_at{limit_sql}",
            params,
        ).fetchall()

    def mark_filtered(self, url: str, reason: str) -> None:
        self._core.execute(
            "UPDATE frontier SET status='filtered', last_error=?, fetched_at=? WHERE url=?",
            (reason, utc_now(), url),
        )
        self._core.commit()

    def filter_frontier(self, min_value_score: int = 16) -> dict[str, int]:
        """Batch-filter pending boilerplate and already-audited low-value URLs."""
        from .scoring import is_obvious_low_value_url

        rows = self._core.execute(
            "SELECT url,source_id,depth,discovered_from FROM frontier WHERE status='pending'"
        ).fetchall()
        filtered: list[tuple[str, str]] = []
        priority_updates: list[tuple[int, str]] = []
        for row in rows:
            url = row["url"]
            if not normalize_url(url):
                filtered.append(("malformed URL placeholder", url))
                continue
            low_value, reason = is_obvious_low_value_url(url)
            if low_value:
                filtered.append((reason, url))
                continue
            saved = self._core.execute(
                "SELECT value_score,page_kind FROM pages WHERE url=?", (url,)
            ).fetchone()
            if int(row["depth"] or 0) > 0 and saved and int(saved["value_score"] or 0) < min_value_score:
                filtered.append(
                    (f"previous score {saved['value_score']} below crawl threshold ({saved['page_kind']})", url)
                )
                continue
            priority_updates.append(
                (url_priority(url, row["source_id"], row["discovered_from"] or ""), url)
            )
        if filtered:
            self._core.executemany(
                "UPDATE frontier SET status='filtered',last_error=?,fetched_at=? WHERE url=?",
                [(reason, utc_now(), url) for reason, url in filtered],
            )
        if priority_updates:
            self._core.executemany("UPDATE frontier SET priority=? WHERE url=?", priority_updates)
        self._core.commit()
        return {"checked": len(rows), "filtered": len(filtered), "reprioritized": len(priority_updates)}

    def mark_processing(self, url: str) -> None:
        self._core.execute(
            "UPDATE frontier SET status='processing', attempts=attempts+1 WHERE url=?", (url,)
        )
        self._core.commit()

    def mark_done(self, url: str, error: str = "") -> None:
        self._core.execute(
            "UPDATE frontier SET status=?, last_error=?, fetched_at=? WHERE url=?",
            ("error" if error else "done", error, utc_now(), url),
        )
        self._core.commit()

    def save_page(
        self, page: PageDocument, source_id: str, depth: int, discovered_from: str = ""
    ) -> Path | None:
        raw_path: Path | None = None
        digest = ""
        if page.html:
            content = page.raw_body or page.html.encode("utf-8", errors="replace")
            digest = sha256_bytes(content)
            raw_path = self.data_dir / "pages" / f"{digest}.html"
            if not raw_path.exists():
                raw_path.write_bytes(content)
            duplicate = self.duplicate_page_url(
                digest,
                page.requested_url,
                prefer_article=page.article is not None,
            )
            if duplicate and not page.duplicate_of:
                page.duplicate_of = duplicate
        if page.status == 200 and page.html and page.article is None:
            old_urls = {page.requested_url, page.final_url, page.canonical_url}
            for old_url in filter(None, old_urls):
                self._core.execute("DELETE FROM article_media WHERE article_url=?", (old_url,))
                self._core.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (old_url,))
                self._core.execute("DELETE FROM articles WHERE url=?", (old_url,))
        self._core.execute(
            """INSERT INTO pages(url,source_id,final_url,status,content_type,fetched_at,depth,discovered_from,
               sha256,raw_path,title,canonical_url,error,blocked_by_robots,page_kind,access_mode,
               value_score,value_tier,score_reasons,published_at,duplicate_of)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET final_url=excluded.final_url,status=excluded.status,
               content_type=excluded.content_type,fetched_at=excluded.fetched_at,depth=excluded.depth,
               discovered_from=excluded.discovered_from,sha256=excluded.sha256,raw_path=excluded.raw_path,
               title=excluded.title,canonical_url=excluded.canonical_url,error=excluded.error,
               blocked_by_robots=excluded.blocked_by_robots,page_kind=excluded.page_kind,
               access_mode=excluded.access_mode,value_score=excluded.value_score,
               value_tier=excluded.value_tier,score_reasons=excluded.score_reasons,
               published_at=excluded.published_at,duplicate_of=excluded.duplicate_of""",
            (
                page.requested_url,
                source_id,
                page.final_url,
                page.status,
                page.content_type,
                page.fetched_at,
                depth,
                discovered_from,
                digest,
                str(raw_path) if raw_path else "",
                page.title,
                page.canonical_url,
                page.error,
                int(page.blocked_by_robots),
                page.page_kind,
                page.access_mode,
                page.value_score,
                page.value_tier,
                json.dumps(page.score_reasons, ensure_ascii=False),
                page.published_at or (page.article.published_at if page.article else ""),
                page.duplicate_of,
            ),
        )
        self._core.commit()
        return raw_path

    def duplicate_page_url(
        self,
        digest: str,
        requested_url: str,
        *,
        prefer_article: bool,
    ) -> str:
        if not digest:
            return ""
        article_clause = (
            """AND EXISTS (
                   SELECT 1 FROM articles a
                   WHERE a.url=p.url
               )"""
            if prefer_article
            else ""
        )
        row = self._core.execute(
            f"""SELECT p.url FROM pages p WHERE p.sha256=? {article_clause}
                ORDER BY p.fetched_at,p.url LIMIT 1""",
            (digest,),
        ).fetchone()
        return str(row[0]) if row and row[0] != requested_url else ""

    def save_asset(
        self,
        *,
        url: str,
        source_url: str,
        body: bytes,
        mime_type: str,
        access_mode: str = "public",
        value_score: int = 0,
        score_reasons: list[str] | None = None,
        error: str = "",
    ) -> Path | None:
        digest = sha256_bytes(body) if body else ""
        local_path: Path | None = None
        error = error or ("empty response body" if not body else "")
        status = "error" if error else "ok"
        if body and not error:
            extension = mimetypes.guess_extension(mime_type.split(";", 1)[0]) or Path(urlsplit(url).path).suffix.lower() or ".bin"
            extension = extension if len(extension) <= 8 else ".bin"
            local_path = self.data_dir / "assets" / digest[:2] / f"{digest}{extension}"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            if not local_path.exists():
                local_path.write_bytes(body)
        self._core.execute(
            """INSERT INTO assets(url,source_url,local_path,mime_type,sha256,size,status,error,fetched_at,
               page_kind,access_mode,value_score,score_reasons)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET source_url=excluded.source_url,
               local_path=excluded.local_path,mime_type=excluded.mime_type,sha256=excluded.sha256,
               size=excluded.size,status=excluded.status,error=excluded.error,fetched_at=excluded.fetched_at,
               page_kind=excluded.page_kind,access_mode=excluded.access_mode,value_score=excluded.value_score,
               score_reasons=excluded.score_reasons""",
            (
                url,
                source_url,
                str(local_path) if local_path else "",
                mime_type,
                digest,
                len(body),
                status,
                error,
                utc_now(),
                "document",
                access_mode,
                value_score,
                json.dumps(score_reasons or [], ensure_ascii=False),
            ),
        )
        self._core.commit()
        return local_path

    def update_page_score(self, url: str, score: Any) -> None:
        self._core.execute(
            """UPDATE pages SET page_kind=?,access_mode=?,value_score=?,value_tier=?,score_reasons=?,published_at=?
               WHERE url=?""",
            (
                score.page_kind,
                score.access_mode,
                score.value_score,
                score.value_tier,
                json.dumps(score.score_reasons, ensure_ascii=False),
                score.published_at,
                url,
            ),
        )
        self._core.commit()

    def save_links(self, source_url: str, source_id: str, links: Iterable[tuple[str, str]]) -> None:
        values = [(source_url, target, source_id, kind, utc_now()) for target, kind in links]
        if not values:
            return
        self._core.executemany(
            "INSERT OR IGNORE INTO links(source_url,target_url,source_id,kind,discovered_at) VALUES(?,?,?,?,?)",
            values,
        )
        self._core.commit()

    def save_article_hint(self, url: str, published_at: str, source_url: str) -> None:
        self._core.execute(
            """INSERT INTO article_hints(url,published_at,source_url,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET published_at=excluded.published_at,
               source_url=excluded.source_url,updated_at=excluded.updated_at""",
            (url, published_at, source_url, utc_now()),
        )
        self._core.commit()

    def article_hint(self, url: str) -> str:
        row = self._core.execute(
            "SELECT published_at FROM article_hints WHERE url=?", (url,)
        ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def article_exists(self, url: str) -> bool:
        return (
            self._core.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone()
            is not None
        )

    def article_alias_exists(self, url: str) -> bool:
        return (
            self._core.execute(
                """SELECT 1
                   FROM pages p JOIN articles a
                     ON a.url=p.url OR a.url=p.final_url
                        OR a.url=p.canonical_url OR a.url=p.duplicate_of
                   WHERE p.url=? LIMIT 1""",
                (url,),
            ).fetchone()
            is not None
        )

    def needs_current_article_repair(self, url: str) -> bool:
        if self.article_exists(url) or self.article_alias_exists(url):
            return False
        page = self._core.execute(
            "SELECT access_mode,page_kind FROM pages WHERE url=?", (url,)
        ).fetchone()
        return page is None or (
            str(page["access_mode"] or "unknown") == "public"
            and str(page["page_kind"] or "unknown") in {"news_article", "article"}
        )

    def save_article(self, article: ArticleDocument) -> str:
        # Reindexing can stage Core deletes before replacing the ORM row.  End
        # that Core transaction before the single pooled engine connection is
        # borrowed by the ORM session.
        self._core.commit()
        sanitize_article_document(article)
        content_hash = sha256_bytes(article.body_text.encode("utf-8", errors="replace"))
        now = utc_now()
        publication_type = classify_publication(
            url=article.url,
            source_id=article.source_id,
            title=article.title,
            category=article.category,
            source_page_url=article.source_page_url,
        )
        article.publication_type = publication_type
        article.classifier_version = CLASSIFIER_VERSION
        with self.database.session_factory.begin() as session:
            self._save_article_record(session, article, content_hash, now)
        self.write_article_bundle(article, content_hash)
        return content_hash

    @staticmethod
    def _save_article_record(
        session: Session,
        article: ArticleDocument,
        content_hash: str,
        now: str,
    ) -> None:
        sanitize_article_document(article)
        raw_json = json.dumps(sanitize_json_value(article.raw_metadata), ensure_ascii=False)
        record = session.get(Article, article.url)
        if record is None:
            record = Article(
                url=article.url,
                source_id=article.source_id,
                first_seen=now,
                last_seen=now,
            )
            session.add(record)
        else:
            record.last_seen = now
        record.source_id = article.source_id
        record.title = article.title
        record.author = article.author
        record.published_at = article.published_at
        record.updated_at = article.updated_at
        record.category = article.category
        record.summary = article.summary
        record.body_html = article.body_html
        record.body_text = article.body_text
        record.body_markdown = article.body_markdown
        record.extraction_method = article.extraction_method
        record.source_page_url = article.source_page_url
        record.raw_json = raw_json
        record.content_hash = content_hash
        record.publication_type = article.publication_type or classify_publication(
            url=article.url,
            source_id=article.source_id,
            title=article.title,
            category=article.category,
            source_page_url=article.source_page_url,
        )
        record.classifier_version = article.classifier_version or CLASSIFIER_VERSION

    def save_article_and_enqueue_for_sync(
        self,
        article: ArticleDocument,
        *,
        run_id: str | None = None,
    ) -> str:
        """Snapshot spool objects and write the article/event in one UoW."""

        self._core.commit()
        sanitize_article_document(article)
        source = self.source_descriptor(article.source_id)
        if source.discovery_only:
            # Discovery sources still populate the local archive, but are not
            # publication ingestion inputs.
            return self.save_article(article)
        content_hash = sha256_bytes(article.body_text.encode("utf-8", errors="replace"))
        publication_type = classify_publication(
            url=article.url,
            source_id=article.source_id,
            title=article.title,
            category=article.category,
            source_page_url=article.source_page_url,
        )
        article.publication_type = publication_type
        article.classifier_version = CLASSIFIER_VERSION
        media_paths = self.media_paths_for_article(article.url)
        asset_paths = self.asset_paths_for_article(article.url, article.source_page_url)
        local_objects = spool_article_objects(
            article,
            self.data_dir,
            media_paths=media_paths,
            asset_paths=asset_paths,
        )
        publication = build_publication(
            article,
            objects=[wire_manifest(item) for item in local_objects],
        )
        self.write_article_bundle(article, content_hash)
        now = utc_now()
        outbox = IngestionOutbox(self.database)
        with self.database.session_factory.begin() as session:
            self._save_article_record(session, article, content_hash, now)
            outbox.enqueue_publication_in_session(
                session,
                publication,
                source=source,
                local_objects=local_objects,
                run_id=run_id,
            )
        return content_hash

    def enqueue_article_for_sync(
        self,
        article: ArticleDocument,
        *,
        run_id: str | None = None,
    ) -> str | None:
        """Snapshot an article and its local objects into the durable outbox."""

        sanitize_article_document(article)
        source = self.source_descriptor(article.source_id)
        if source.discovery_only:
            return None
        media_paths = self.media_paths_for_article(article.url)
        asset_paths = self.asset_paths_for_article(article.url, article.source_page_url)
        return IngestionOutbox(self.database).enqueue_article(
            article,
            self.data_dir,
            source=source,
            media_paths=media_paths,
            asset_paths=asset_paths,
            run_id=run_id,
        )

    def media_paths_for_article(self, article_url: str) -> dict[str, tuple[str, str]]:
        with self.database.session_factory() as session:
            # ``media.article_url`` identifies the page that first downloaded
            # an object, while ``article_media`` records every article that
            # references it.  Shared media can therefore have a NULL owner
            # URL and still be a valid object for this article.
            rows = session.scalars(
                select(Media)
                .outerjoin(ArticleMedia, ArticleMedia.image_url == Media.url)
                .where(
                    or_(
                        Media.article_url == article_url,
                        ArticleMedia.article_url == article_url,
                    )
                )
                .order_by(Media.url)
            ).all()
            return {
                str(row.url): (str(row.local_path), str(row.mime_type or "application/octet-stream"))
                for row in rows
                if row.local_path and Path(row.local_path).is_file()
            }

    def asset_paths_for_article(
        self,
        article_url: str,
        source_page_url: str = "",
    ) -> dict[str, tuple[str, str]]:
        """Return only downloaded linked assets for one article snapshot."""

        source_urls = {value for value in (article_url, source_page_url) if value}
        if not source_urls:
            return {}
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(Asset)
                .where(Asset.source_url.in_(source_urls), Asset.status == "ok")
                .order_by(Asset.url)
            ).all()
            return {
                str(row.url): (str(row.local_path), str(row.mime_type or "application/octet-stream"))
                for row in rows
                if row.local_path and Path(row.local_path).is_file()
            }

    def sync_article_page(self, after_url: str = "", limit: int = 100) -> list[ArticleDocument]:
        """Read a bounded keyset page of article snapshots for sync backfill."""

        return [
            snapshot.article
            for snapshot in self.sync_article_snapshot_page(after_url, limit)
        ]

    def sync_article_snapshot_page(
        self, after_url: str = "", limit: int = 100
    ) -> list[ArticleSyncSnapshot]:
        """Read articles and linked local objects with bounded bulk queries.

        The article URL is the keyset cursor.  Every relationship query is
        scoped to this page, so a backfill does not grow its query count with
        the number of articles in a chunk.  Files are checked only after the
        read-only database session closes; callers may therefore spool and
        hash objects without holding a database transaction.
        """

        if limit < 1:
            raise ValueError("sync backfill limit must be positive")
        with self.database.session_factory() as session:
            query = select(Article).order_by(Article.url).limit(limit)
            if after_url:
                query = query.where(Article.url > after_url)
            rows = session.scalars(query).all()
            if not rows:
                return []

            article_urls = [row.url for row in rows]
            image_rows = session.scalars(
                select(ArticleMedia)
                .where(ArticleMedia.article_url.in_(article_urls))
                .order_by(ArticleMedia.article_url, ArticleMedia.image_url)
            ).all()
            media_rows = session.scalars(
                select(Media)
                .outerjoin(ArticleMedia, ArticleMedia.image_url == Media.url)
                .where(
                    or_(
                        Media.article_url.in_(article_urls),
                        ArticleMedia.article_url.in_(article_urls),
                    )
                )
                .order_by(Media.url)
            ).unique().all()

            source_urls = {
                url
                for row in rows
                for url in (row.url, row.source_page_url or row.url)
                if url
            }
            asset_rows = []
            if source_urls:
                asset_rows = session.scalars(
                    select(Asset)
                    .where(Asset.source_url.in_(source_urls), Asset.status == "ok")
                    .order_by(Asset.url)
                ).all()

            # Copy all scalar fields while the session is alive, then close it
            # before checking local files in the maps below.
            article_values = [
                {
                    "url": row.url,
                    "source_id": row.source_id,
                    "title": row.title or "",
                    "author": row.author or "",
                    "published_at": row.published_at or "",
                    "updated_at": row.updated_at or "",
                    "category": row.category or "",
                    "summary": row.summary or "",
                    "body_html": row.body_html or "",
                    "body_text": row.body_text or "",
                    "body_markdown": row.body_markdown or "",
                    "extraction_method": row.extraction_method or "",
                    "source_page_url": row.source_page_url or row.url,
                    "raw_json": row.raw_json,
                    "publication_type": row.publication_type,
                    "classifier_version": row.classifier_version,
                }
                for row in rows
            ]
            image_values = [
                {
                    "article_url": image.article_url,
                    "url": image.image_url,
                    "alt": image.alt or "",
                    "title": image.title or "",
                    "caption": image.caption or "",
                }
                for image in image_rows
            ]
            media_values = [
                {
                    "url": media.url,
                    "article_url": media.article_url,
                    "local_path": media.local_path,
                    "mime_type": media.mime_type,
                }
                for media in media_rows
            ]
            asset_values = [
                {
                    "url": asset.url,
                    "source_url": asset.source_url,
                    "local_path": asset.local_path,
                    "mime_type": asset.mime_type,
                }
                for asset in asset_rows
            ]

        images_by_article: dict[str, list[ImageRef]] = {}
        media_urls_by_article: dict[str, set[str]] = {
            url: set() for url in article_urls
        }
        for image in image_values:
            article_url = str(image["article_url"])
            images_by_article.setdefault(article_url, []).append(
                ImageRef(
                    url=str(image["url"]),
                    alt=str(image["alt"]),
                    title=str(image["title"]),
                    caption=str(image["caption"]),
                    article_url=article_url,
                )
            )
            media_urls_by_article.setdefault(article_url, set()).add(str(image["url"]))

        media_by_url = {str(media["url"]): media for media in media_values}
        for media in media_values:
            article_url = media["article_url"]
            if article_url in media_urls_by_article:
                media_urls_by_article[article_url].add(str(media["url"]))

        assets_by_source: dict[str, list[dict[str, Any]]] = {}
        for asset in asset_values:
            assets_by_source.setdefault(str(asset["source_url"]), []).append(asset)

        result: list[ArticleSyncSnapshot] = []
        for values in article_values:
            article_url = str(values["url"])
            source_page_url = str(values["source_page_url"])
            raw_metadata: dict[str, Any] = {}
            if values["raw_json"]:
                try:
                    value = json.loads(values["raw_json"])
                    if isinstance(value, dict):
                        raw_metadata = value
                except (TypeError, ValueError):
                    pass
            article = ArticleDocument(
                url=article_url,
                source_id=str(values["source_id"]),
                title=str(values["title"]),
                author=str(values["author"]),
                published_at=str(values["published_at"]),
                updated_at=str(values["updated_at"]),
                category=str(values["category"]),
                summary=str(values["summary"]),
                body_html=str(values["body_html"]),
                body_text=str(values["body_text"]),
                body_markdown=str(values["body_markdown"]),
                extraction_method=str(values["extraction_method"]),
                source_page_url=source_page_url,
                raw_metadata=raw_metadata,
                images=images_by_article.get(article_url, []),
                publication_type=values["publication_type"],
                classifier_version=values["classifier_version"],
            )
            media_paths: dict[str, tuple[str, str]] = {}
            for media_url in sorted(media_urls_by_article.get(article_url, ())):
                media = media_by_url.get(media_url)
                if not media or not media["local_path"]:
                    continue
                path = Path(str(media["local_path"]))
                if path.is_file():
                    media_paths[media_url] = (
                        str(path),
                        str(media["mime_type"] or "application/octet-stream"),
                    )

            asset_paths: dict[str, tuple[str, str]] = {}
            for source_url in (article_url, source_page_url):
                for asset in assets_by_source.get(source_url, ()):
                    if not asset["local_path"]:
                        continue
                    path = Path(str(asset["local_path"]))
                    if path.is_file():
                        asset_paths[str(asset["url"])] = (
                            str(path),
                            str(asset["mime_type"] or "application/octet-stream"),
                        )

            result.append(
                ArticleSyncSnapshot(
                    article=article,
                    media_paths=media_paths,
                    asset_paths=asset_paths,
                )
            )
        return result

    def write_article_bundle(
        self, article: ArticleDocument, content_hash: str | None = None
    ) -> None:
        sanitize_article_document(article)
        content_hash = content_hash or sha256_bytes(
            article.body_text.encode("utf-8", errors="replace")
        )
        archive_key = article_bundle_key(article.url)
        base = self.data_dir / "articles" / archive_key
        payload = {
            "archive_key": archive_key,
            "content_hash": content_hash,
            "url": article.url,
            "source_id": article.source_id,
            "title": article.title,
            "author": article.author,
            "published_at": article.published_at,
            "updated_at": article.updated_at,
            "category": article.category,
            "summary": article.summary,
            "body_text": article.body_text,
            "body_markdown": article.body_markdown,
            "extraction_method": article.extraction_method,
            "source_page_url": article.source_page_url,
            "raw_metadata": article.raw_metadata,
            "images": [
                image.__dict__
                if hasattr(image, "__dict__")
                else {
                    "url": image.url,
                    "alt": image.alt,
                    "title": image.title,
                    "caption": image.caption,
                }
                for image in article.images
            ],
        }
        (base.with_suffix(".json")).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (base.with_suffix(".html")).write_text(article.body_html, encoding="utf-8")

    def rebuild_article_bundles(self) -> dict[str, int]:
        """Rebuild every URL-specific JSON/HTML article archive from SQLite."""
        images_by_article: dict[str, list[ImageRef]] = {}
        for row in self._core.execute(
            """SELECT article_url,image_url,alt,title,caption
               FROM article_media ORDER BY article_url,created_at,image_url"""
        ):
            images_by_article.setdefault(str(row["article_url"]), []).append(
                ImageRef(
                    url=str(row["image_url"]),
                    alt=str(row["alt"] or ""),
                    title=str(row["title"] or ""),
                    caption=str(row["caption"] or ""),
                    article_url=str(row["article_url"]),
                )
            )
        rebuilt = 0
        for row in self._core.execute("SELECT * FROM articles ORDER BY url"):
            try:
                raw_metadata = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                raw_metadata = {}
            article = ArticleDocument(
                url=str(row["url"]),
                source_id=str(row["source_id"]),
                title=str(row["title"] or ""),
                author=str(row["author"] or ""),
                published_at=str(row["published_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
                category=str(row["category"] or ""),
                summary=str(row["summary"] or ""),
                body_html=str(row["body_html"] or ""),
                body_text=str(row["body_text"] or ""),
                body_markdown=str(row["body_markdown"] or ""),
                extraction_method=str(row["extraction_method"] or ""),
                source_page_url=str(row["source_page_url"] or ""),
                raw_metadata=raw_metadata if isinstance(raw_metadata, dict) else {},
                images=images_by_article.get(str(row["url"]), []),
            )
            self.write_article_bundle(article, str(row["content_hash"] or ""))
            rebuilt += 1
        return {"rebuilt": rebuilt}

    def save_media(
        self,
        image: ImageRef,
        body: bytes,
        content_type: str,
        article_url: str,
        source_page_url: str,
        error: str = "",
    ) -> Path | None:
        digest = sha256_bytes(body) if body else ""
        local_path: Path | None = None
        status = "error" if error else "ok"
        if body and not error:
            extension = (
                mimetypes.guess_extension(content_type.split(";", 1)[0])
                or Path(urlsplit(image.url).path).suffix.lower()
                or ".bin"
            )
            extension = extension if len(extension) <= 8 else ".bin"
            local_path = self.data_dir / "media" / digest[:2] / f"{digest}{extension}"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            if not local_path.exists():
                local_path.write_bytes(body)
        self._core.execute(
            """INSERT INTO media(url,article_url,source_page_url,local_path,mime_type,sha256,size,alt,title,caption,status,error,fetched_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET article_url=excluded.article_url,source_page_url=excluded.source_page_url,
               local_path=excluded.local_path,mime_type=excluded.mime_type,sha256=excluded.sha256,size=excluded.size,
               alt=excluded.alt,title=excluded.title,caption=excluded.caption,status=excluded.status,error=excluded.error,
               fetched_at=excluded.fetched_at""",
            (
                image.url,
                article_url,
                source_page_url,
                str(local_path) if local_path else "",
                content_type,
                digest,
                len(body),
                image.alt,
                image.title,
                image.caption,
                status,
                error,
                utc_now(),
            ),
        )
        self._core.execute(
            """INSERT INTO article_media(article_url,image_url,local_path,alt,title,caption,created_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(article_url,image_url) DO UPDATE SET
               local_path=excluded.local_path,alt=excluded.alt,title=excluded.title,
               caption=excluded.caption""",
            (
                article_url,
                image.url,
                str(local_path) if local_path else "",
                image.alt,
                image.title,
                image.caption,
                utc_now(),
            ),
        )
        self._core.commit()
        return local_path

    def link_media(self, image: ImageRef, article_url: str, source_page_url: str = "") -> None:
        """Attach an already stored media URL to another article reference."""
        row = self._core.execute(
            "SELECT local_path FROM media WHERE url=?", (image.url,)
        ).fetchone()
        if not row:
            return
        self._core.execute(
            """INSERT INTO article_media(article_url,image_url,local_path,alt,title,caption,created_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(article_url,image_url) DO UPDATE SET
               local_path=excluded.local_path,alt=excluded.alt,title=excluded.title,
               caption=excluded.caption""",
            (
                article_url,
                image.url,
                row["local_path"] or "",
                image.alt,
                image.title,
                image.caption,
                utc_now(),
            ),
        )
        self._core.commit()

    def _blocked_host_article_urls(
        self, source_id: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Return (url, source_id, blocked_host) for articles on blocked hosts."""
        params: list[str] = []
        where = ""
        if source_id:
            where = "WHERE id=?"
            params.append(source_id)
        rows = self._core.execute(
            f"SELECT id, blocked_hosts FROM sources {where}", params
        ).fetchall()
        results: list[tuple[str, str, str]] = []
        for source_row in rows:
            sid = str(source_row["id"])
            blocked = json.loads(source_row["blocked_hosts"] or "[]")
            if not blocked:
                continue
            article_rows = self._core.execute(
                "SELECT url FROM articles WHERE source_id=?", (sid,)
            ).fetchall()
            for article_row in article_rows:
                url = str(article_row["url"])
                if host_matches(url, blocked):
                    host = (urlsplit(url).hostname or "").lower()
                    for bh in blocked:
                        if host == bh.lower() or host.endswith("." + bh.lower()):
                            results.append((url, sid, bh))
                            break
        return results

    def cleanup_blocked_host_articles(
        self, source_id: str | None = None, commit: bool = False
    ) -> dict[str, Any]:
        """Remove articles whose URL host is blocked for their source.

        Blocked hosts are third-party services/mirrors that were discovered from
        a source but should not have been indexed as articles. The media files
        themselves are kept; only the database links are cleared.
        """
        candidates = self._blocked_host_article_urls(source_id)
        removed = 0
        urls = {url for url, _, _ in candidates}
        if commit and urls:
            for url in urls:
                self._core.execute("DELETE FROM article_media WHERE article_url=?", (url,))
                self._core.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (url,))
                removed += self._core.execute("DELETE FROM articles WHERE url=?", (url,)).rowcount
            self._core.commit()
        return {
            "candidate_urls": len(urls),
            "removed_articles": removed if commit else 0,
            "dry_run": not commit,
        }

    def _orphan_media_rows(self) -> list[RowMapping]:
        """Return media rows with no article_media relationship."""
        return self._core.execute(
            """SELECT m.url, m.article_url, m.local_path, m.status
               FROM media m
               LEFT JOIN article_media am ON am.image_url=m.url
               WHERE am.image_url IS NULL"""
        ).fetchall()

    def _relink_orphan_media(self, rows: list[RowMapping]) -> tuple[int, int]:
        """Try to reconnect orphan media rows to their article bundles.

        Returns (relinked_count, dangling_count) where dangling means the
        media.article_url points to a missing article.
        """
        relinked = 0
        dangling = 0
        for row in rows:
            article_url = row["article_url"] or ""
            if not article_url:
                continue
            article_row = self._core.execute(
                "SELECT 1 FROM articles WHERE url=?", (article_url,)
            ).fetchone()
            if not article_row:
                dangling += 1
                self._core.execute(
                    "UPDATE media SET article_url=NULL WHERE url=?", (row["url"],)
                )
                continue
            bundle = article_bundle_path(self.data_dir, article_url)
            if not bundle.exists():
                continue
            try:
                payload = json.loads(bundle.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            images = payload.get("images", []) if isinstance(payload, dict) else []
            for value in images:
                if not isinstance(value, dict):
                    continue
                if str(value.get("url") or "") != row["url"]:
                    continue
                self.link_media(
                    ImageRef(
                        url=row["url"],
                        alt=str(value.get("alt") or ""),
                        title=str(value.get("title") or ""),
                        caption=str(value.get("caption") or ""),
                        article_url=article_url,
                    ),
                    article_url,
                    "",
                )
                relinked += 1
                break
        return relinked, dangling

    def _orphan_media_breakdown(self, rows: list[RowMapping]) -> dict[str, int]:
        """Categorize orphan media rows for reporting.

        - stale_linkage: article_url points to an existing article but the
          article_media row is missing.
        - dangling_linkage: article_url points to a removed article.
        - unreferenced: article_url is NULL, so the media was never linked.
        """
        stale = 0
        dangling = 0
        unreferenced = 0
        article_urls = {row["article_url"] for row in rows if row["article_url"]}
        existing: set[str] = set()
        if article_urls:
            placeholders = ",".join("?" for _ in article_urls)
            existing = {
                str(r[0])
                for r in self._core.execute(
                    f"SELECT url FROM articles WHERE url IN ({placeholders})",
                    tuple(article_urls),
                ).fetchall()
            }
        for row in rows:
            article_url = row["article_url"] or ""
            if not article_url:
                unreferenced += 1
            elif article_url in existing:
                stale += 1
            else:
                dangling += 1
        return {
            "stale_linkage": stale,
            "dangling_linkage": dangling,
            "unreferenced": unreferenced,
        }

    def cleanup_orphan_media(self, commit: bool = False) -> dict[str, Any]:
        """Repair or remove media rows that are not linked to any article.

        Orphans usually come from deleted articles or pre-relationship media.
        First we try to relink media whose article_url still points to a saved
        article bundle. Remaining unlinked rows are then removed from the media
        table; actual files on disk are left in place so the cleanup is safe to
        reverse by re-running image downloads.
        """
        rows = self._orphan_media_rows()
        breakdown = self._orphan_media_breakdown(rows)
        relinked = 0
        dangling = 0
        deleted = 0
        if commit and rows:
            relinked, dangling = self._relink_orphan_media(rows)
            # After relinking, any row still without article_media is unreferenced.
            still_orphan = self._orphan_media_rows()
            for row in still_orphan:
                self._core.execute("DELETE FROM media WHERE url=?", (row["url"],))
                deleted += 1
            self._core.commit()
        return {
            "orphan_media": len(rows),
            **breakdown,
            "relinked": relinked if commit else 0,
            "dangling_cleared": dangling if commit else 0,
            "deleted": deleted if commit else 0,
            "dry_run": not commit,
        }

    def reassign_articles_by_host(
        self, source_id: str | None = None, commit: bool = False
    ) -> dict[str, Any]:
        """Reassign articles to the source whose allowed_hosts match their URL host.

        Articles whose host does not match any configured source are removed,
        because they were either discovered through a redirect or stored before
        the host allowlist was enforced.
        """
        host_to_source: dict[str, str] = {}
        for row in self._core.execute("SELECT id, allowed_hosts FROM sources"):
            sid = str(row["id"])
            for host in json.loads(row["allowed_hosts"] or "[]"):
                host_to_source[str(host).lower().lstrip("*.")] = sid

        params: list[str] = []
        where = ""
        if source_id:
            where = "WHERE source_id=?"
            params.append(source_id)
        rows = self._core.execute(
            f"SELECT url, source_id FROM articles {where}", tuple(params)
        ).fetchall()

        reassigned = 0
        deleted = 0
        for row in rows:
            url = str(row["url"])
            current = str(row["source_id"])
            host = (urlsplit(url).hostname or "").lower()
            matched = None
            for allowed, sid in host_to_source.items():
                if host == allowed or host.endswith("." + allowed):
                    matched = sid
                    break
            if matched and matched != current:
                if commit:
                    self._core.execute(
                        "UPDATE articles SET source_id=? WHERE url=?", (matched, url)
                    )
                reassigned += 1
            elif not matched:
                if commit:
                    self._core.execute("DELETE FROM article_media WHERE article_url=?", (url,))
                    self._core.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (url,))
                    deleted += self._core.execute("DELETE FROM articles WHERE url=?", (url,)).rowcount
                else:
                    deleted += 1
        if commit and (reassigned or deleted):
            self._core.commit()
        return {
            "scanned": len(rows),
            "reassigned": reassigned,
            "deleted": deleted,
            "dry_run": not commit,
        }

    def trim_excess_images(
        self,
        source_caps: dict[str, int] | None = None,
        commit: bool = False,
    ) -> dict[str, Any]:
        """Remove article-media relationships that exceed per-source caps.

        Crawl-time image caps prevent new articles from downloading too many
        images, but historical crawls may still have more relationships. This
        pass deletes the excess relationships so that ``cleanup_orphan_media``
        can remove the now-unreferenced media rows.
        """
        deleted = 0
        if not source_caps:
            return {"deleted": 0, "dry_run": not commit}
        for sid, cap in source_caps.items():
            if cap <= 0:
                continue
            rows = self._core.execute(
                """
                SELECT a.url AS article_url, a.content_hash
                FROM articles a
                JOIN article_media am ON am.article_url=a.url
                WHERE a.source_id=?
                GROUP BY a.url
                HAVING COUNT(am.image_url) > ?
                """,
                (sid, cap),
            ).fetchall()
            for row in rows:
                article_url = row["article_url"]
                # Keep the first ``cap`` rows ordered by creation time; this
                # mirrors the original extraction order.
                to_delete = self._core.execute(
                    """
                    SELECT image_url FROM article_media
                    WHERE article_url=?
                    ORDER BY created_at, image_url
                    LIMIT -1 OFFSET ?
                    """,
                    (article_url, cap),
                ).fetchall()
                removed_urls = {str(del_row["image_url"]) for del_row in to_delete}
                for del_row in to_delete:
                    if commit:
                        self._core.execute(
                            "DELETE FROM article_media WHERE article_url=? AND image_url=?",
                            (article_url, del_row["image_url"]),
                        )
                        self._core.execute(
                            "UPDATE media SET article_url=NULL WHERE url=? AND article_url=?",
                            (del_row["image_url"], article_url),
                        )
                    deleted += 1
                if commit and removed_urls:
                    bundle = article_bundle_path(self.data_dir, article_url)
                    if bundle.exists():
                        try:
                            payload = json.loads(bundle.read_text(encoding="utf-8"))
                        except (OSError, UnicodeError, json.JSONDecodeError):
                            payload = None
                        if isinstance(payload, dict) and payload.get("url") == article_url:
                            images = payload.get("images")
                            if isinstance(images, list):
                                payload["images"] = [
                                    image
                                    for image in images
                                    if not isinstance(image, dict)
                                    or str(image.get("url") or "") not in removed_urls
                                ]
                                bundle.write_text(
                                    json.dumps(payload, ensure_ascii=False, indent=2),
                                    encoding="utf-8",
                                )
        if commit and deleted:
            self._core.commit()
        return {"deleted": deleted, "dry_run": not commit}

    def cleanup_data(
        self,
        source_id: str | None = None,
        commit: bool = False,
        source_caps: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Run cleanup passes for blocked-host articles, orphan media, and excess images."""
        return {
            "blocked_host_articles": self.cleanup_blocked_host_articles(source_id, commit),
            "misassigned_host_articles": self.reassign_articles_by_host(source_id, commit),
            "excess_images": self.trim_excess_images(source_caps, commit),
            "orphan_media": self.cleanup_orphan_media(commit),
            "commit": commit,
        }

    def failure(self, url: str, source_id: str, error: str, status: int | None = None) -> None:
        self._core.execute(
            "INSERT INTO failures(url,source_id,error,status,last_seen) VALUES(?,?,?,?,?)",
            (url, source_id, error, status, utc_now()),
        )
        self._core.commit()

    def stats(self) -> dict[str, int]:
        names = {
            "sources": "sources",
            "frontier": "frontier",
            "pages": "pages",
            "articles": "articles",
            "media": "media",
            "assets": "assets",
            "article_media": "article_media",
            "failures": "failures",
        }
        result = {
            name: int(self._core.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for name, table in names.items()
        }
        result["frontier_filtered"] = int(
            self._core.execute("SELECT COUNT(*) FROM frontier WHERE status='filtered'").fetchone()[0]
        )
        result["pages_indexable"] = int(
            self._core.execute("SELECT COUNT(*) FROM pages WHERE value_score >= 16").fetchone()[0]
        )
        return result

    def export_jsonl(self, output: str | Path) -> Path:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self._core.execute("SELECT * FROM articles ORDER BY published_at DESC, url").fetchall()
        with target.open("w", encoding="utf-8") as handle:
            for row in rows:
                item = dict(row)
                item["images"] = [
                    dict(image)
                    for image in self._core.execute(
                        "SELECT image_url AS url,local_path,alt,title,caption FROM article_media WHERE article_url=? ORDER BY image_url",
                        (row["url"],),
                    ).fetchall()
                ]
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return target

    def source_newest_dates(self, source_ids: set[str]) -> dict[str, datetime]:
        """Return the newest article date per source, used for incremental crawls.

        Only sources that already have dated articles get a cutoff; sources with
        no previous articles are crawled without a date restriction.
        """
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        rows = self._core.execute(
            f"""SELECT source_id, MAX(published_at) AS newest
                FROM articles
                WHERE source_id IN ({placeholders}) AND published_at IS NOT NULL AND published_at != ''
                GROUP BY source_id""",
            tuple(sorted(source_ids)),
        ).fetchall()
        result: dict[str, datetime] = {}
        now = datetime.now().astimezone()
        for row in rows:
            value = str(row["newest"])
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            # Future-dated articles (parsing artifacts or scheduled posts) must
            # not push the incremental cutoff past now, or we would skip current
            # content that should be refreshed.
            if parsed > now:
                parsed = now
            result[str(row["source_id"])] = parsed
        return result

    def source_report(self, output: str | Path | None = None) -> list[dict[str, Any]]:
        today = datetime.now().astimezone().date().isoformat()
        cutoff = (datetime.now().astimezone() - timedelta(days=365)).date().isoformat()
        rows = self._core.execute(
            """SELECT s.id,s.name,s.organization_level,s.allowed_hosts,
                      (SELECT COUNT(*) FROM pages p WHERE p.source_id=s.id) AS pages,
                      (SELECT COUNT(*) FROM pages p WHERE p.source_id=s.id AND
                        (lower(p.url) LIKE '%news%' OR lower(p.url) LIKE '%notice%' OR
                         lower(p.url) LIKE '%announcement%' OR lower(p.url) LIKE '%xwdt%' OR
                         lower(p.url) LIKE '%tzgg%' OR p.page_kind IN ('news_article','news_listing'))) AS publication_pages,
                      (SELECT COUNT(*) FROM pages p WHERE p.source_id=s.id AND p.value_score >= 16
                        AND (p.duplicate_of IS NULL OR p.duplicate_of='')) AS indexable_pages,
                      (SELECT COUNT(*) FROM pages p WHERE p.source_id=s.id AND p.page_kind='news_article'
                        AND p.value_score >= 40 AND (p.duplicate_of IS NULL OR p.duplicate_of='')) AS indexable_news_pages,
                      (SELECT COUNT(*) FROM articles a WHERE a.source_id=s.id) AS articles,
                      (SELECT MAX(a.published_at) FROM articles a WHERE a.source_id=s.id) AS latest_published_at,
                      (SELECT COUNT(*) FROM articles a WHERE a.source_id=s.id AND a.published_at >= ? AND a.published_at <= ?) AS recent_articles,
                      (SELECT COUNT(*) FROM frontier f WHERE f.source_id=s.id AND f.status='pending') AS pending,
                      (SELECT COUNT(*) FROM pages p WHERE p.source_id=s.id AND p.error IS NOT NULL AND p.error != '') AS failed_pages,
                      (SELECT COUNT(*) FROM pages p WHERE p.source_id=s.id AND p.blocked_by_robots=1) AS robots_blocked
               FROM sources s ORDER BY s.id""",
            (cutoff, today),
        ).fetchall()
        report = []
        for row in rows:
            item = dict(row)
            item["allowed_hosts"] = json.loads(item["allowed_hosts"] or "[]")
            item["has_publication"] = bool(item["articles"] or item["publication_pages"])
            item["has_publish_channel"] = item["has_publication"]
            item["recent_365d"] = bool(item["recent_articles"])
            item["scope"] = (
                "supplemental"
                if item["id"].startswith("supplemental-")
                else "unit"
                if item["id"].startswith("unit-")
                else "core"
            )
            report.append(item)
        if output:
            target = Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def rescore_pages(
        self,
        batch_size: int = 1000,
        page_urls: set[str] | None = None,
    ) -> dict[str, int]:
        """Rescore the saved page inventory without reparsing or networking.

        This fast pass is useful for a large existing archive: it uses saved
        titles/article metadata, URL signals, status and duplicate hashes.  A
        later ``reindex`` can still perform the more expensive HTML reparse
        when extraction rules themselves change.
        """
        from .scoring import document_asset_url, score_page

        duplicate_rows = self._core.execute(
            """SELECT sha256,url AS first_url FROM (
                   SELECT sha256,url,
                          count(*) OVER (PARTITION BY sha256) AS copies,
                          row_number() OVER (
                              PARTITION BY sha256
                              ORDER BY CASE WHEN EXISTS (
                                  SELECT 1 FROM articles a
                                  WHERE a.url=ranked.url
                              ) THEN 0 ELSE 1 END,
                              fetched_at,url
                          ) AS keeper_rank
                   FROM pages ranked WHERE sha256 IS NOT NULL AND sha256 != ''
               ) WHERE copies > 1 AND keeper_rank=1"""
        ).fetchall()
        duplicate_first = {str(row["sha256"]): str(row["first_url"]) for row in duplicate_rows}
        page_query = """SELECT p.*,
                      (SELECT a.title FROM articles a
                       WHERE a.url=p.url OR a.url=p.final_url OR a.url=p.canonical_url LIMIT 1) AS article_title,
                      (SELECT a.body_text FROM articles a
                       WHERE a.url=p.url OR a.url=p.final_url OR a.url=p.canonical_url LIMIT 1) AS article_body,
                      (SELECT a.published_at FROM articles a
                       WHERE a.url=p.url OR a.url=p.final_url OR a.url=p.canonical_url LIMIT 1) AS article_published
               FROM pages p"""
        page_params: tuple[str, ...] = ()
        if page_urls:
            placeholders = ",".join("?" for _ in page_urls)
            page_query += f" WHERE p.url IN ({placeholders})"
            page_params = tuple(sorted(page_urls))
        rows = self._core.execute(page_query, page_params).fetchall()
        updates: list[tuple[Any, ...]] = []
        scored = 0
        indexable = 0
        for row in rows:
            keeper = duplicate_first.get(str(row["sha256"]), "")
            duplicate_of = keeper if keeper and row["url"] != keeper else ""
            article_body = row["article_body"] or ""
            result = score_page(
                url=row["url"],
                final_url=row["final_url"] or row["url"],
                title=row["article_title"] or row["title"] or "",
                body_text=article_body[:8000],
                status=int(row["status"] or 0),
                content_type=row["content_type"] or "",
                has_article=bool(row["article_body"] is not None),
                published_at=row["article_published"] or row["published_at"] or "",
                document_link_count=int(
                    document_asset_url(row["final_url"] or row["url"])
                    and "html" not in (row["content_type"] or "").lower()
                ),
                duplicate=bool(duplicate_of),
                blocked_by_robots=bool(row["blocked_by_robots"]),
            )
            updates.append(
                (
                    result.page_kind,
                    result.access_mode,
                    result.value_score,
                    result.value_tier,
                    json.dumps(result.score_reasons, ensure_ascii=False),
                    result.published_at,
                    duplicate_of,
                    row["url"],
                )
            )
            scored += 1
            indexable += int(result.value_score >= 16 and not duplicate_of)
            if len(updates) >= batch_size:
                self._core.executemany(
                    """UPDATE pages SET page_kind=?,access_mode=?,value_score=?,value_tier=?,
                       score_reasons=?,published_at=?,duplicate_of=? WHERE url=?""",
                    updates,
                )
                self._core.commit()
                updates.clear()
        if updates:
            self._core.executemany(
                """UPDATE pages SET page_kind=?,access_mode=?,value_score=?,value_tier=?,
                   score_reasons=?,published_at=?,duplicate_of=? WHERE url=?""",
                updates,
            )
            self._core.commit()
        self._core.execute(
            """UPDATE assets SET value_score=(SELECT p.value_score FROM pages p WHERE p.url=assets.url),
               score_reasons=(SELECT p.score_reasons FROM pages p WHERE p.url=assets.url),
               access_mode=(SELECT p.access_mode FROM pages p WHERE p.url=assets.url)
               WHERE EXISTS (SELECT 1 FROM pages p WHERE p.url=assets.url)"""
        )
        self._core.commit()
        return {"scored": scored, "indexable": indexable, "duplicates": len(duplicate_rows)}

    def backfill_assets_from_pages(self) -> dict[str, int]:
        """Normalize legacy document responses into the assets inventory."""
        from .scoring import document_asset_url

        scanned = 0
        saved = 0
        for row in self._core.execute(
            """SELECT url,final_url,discovered_from,raw_path,content_type,access_mode,value_score,score_reasons
               FROM pages WHERE status=200 AND raw_path IS NOT NULL AND raw_path != ''"""
        ).fetchall():
            target = row["final_url"] or row["url"]
            content_type = (row["content_type"] or "").lower()
            if (
                not document_asset_url(target)
                or content_type.startswith(("text/html", "application/xhtml", "text/xml", "application/xml", "application/rss"))
            ):
                continue
            path = Path(row["raw_path"])
            if not path.exists():
                candidate = self.db_path.parent / path
                if candidate.exists():
                    path = candidate
            if not path.exists():
                continue
            try:
                body = path.read_bytes()
            except OSError:
                continue
            scanned += 1
            try:
                reasons = json.loads(row["score_reasons"] or "[]")
            except json.JSONDecodeError:
                reasons = []
            self.save_asset(
                url=row["url"],
                source_url=row["discovered_from"] or "",
                body=body,
                mime_type=row["content_type"] or "application/octet-stream",
                access_mode=row["access_mode"] or "unknown",
                value_score=int(row["value_score"] or 0),
                score_reasons=reasons if isinstance(reasons, list) else [],
            )
            saved += 1
        return {"scanned": scanned, "saved": saved}

    def reindex_extractions(
        self,
        source_ids: set[str] | None = None,
        source_caps: dict[str, int] | None = None,
        page_urls: set[str] | None = None,
    ) -> dict[str, int]:
        """Re-run the parser over saved HTML without making network requests.

        Source and page subsets let targeted repairs finish without rescanning
        the entire archive after an extraction or classification change.
        """
        from .crawl import _decode
        from .extract import extract_page
        from .scoring import document_asset_url, score_page

        scanned = 0
        articles = 0
        removed = 0
        document_articles_removed = 0
        max_reindex_bytes = 8 * 1024 * 1024
        # Avoid one duplicate lookup query per page.  The archive is large
        # enough that the old ``duplicate_page_url`` call turned reindexing
        # into an hours-long sequence of random SQLite reads.  Build a small
        # first-seen map while streaming the page table instead.
        duplicate_first: dict[str, str] = {}
        duplicate_seen: dict[str, str] = {}
        for duplicate_row in self._core.execute(
            """SELECT p.sha256,p.url FROM pages p
               WHERE p.sha256 IS NOT NULL AND p.sha256 != ''
               ORDER BY CASE WHEN EXISTS (
                   SELECT 1 FROM articles a
                   WHERE a.url=p.url
               ) THEN 0 ELSE 1 END,
               p.fetched_at,p.url"""
        ):
            digest = str(duplicate_row["sha256"])
            url = str(duplicate_row["url"])
            if digest in duplicate_seen:
                duplicate_first.setdefault(digest, duplicate_seen[digest])
            else:
                duplicate_seen[digest] = url
        page_query = "SELECT * FROM pages WHERE status=200 AND raw_path IS NOT NULL AND raw_path != ''"
        page_params: tuple[str, ...] = ()
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            page_query += f" AND source_id IN ({placeholders})"
            page_params = tuple(sorted(source_ids))
        if page_urls:
            placeholders = ",".join("?" for _ in page_urls)
            page_query += f" AND url IN ({placeholders})"
            page_params += tuple(sorted(page_urls))
        rows = self._core.execute(page_query, page_params)
        for row in rows:
            content_type = (row["content_type"] or "").lower()
            if content_type and "html" not in content_type and "xhtml" not in content_type:
                continue
            raw_path = Path(row["raw_path"])
            if not raw_path.exists():
                candidate = self.db_path.parent / raw_path
                if candidate.exists():
                    raw_path = candidate
            if not raw_path.exists():
                continue
            scanned += 1
            try:
                body = raw_path.read_bytes()
                if looks_like_asset(row["final_url"] or row["url"]) or looks_like_binary(body):
                    document_url = row["final_url"] or row["url"]
                    document_title = Path(urlsplit(document_url).path).name or "document"
                    document_result = score_page(
                        url=row["url"],
                        final_url=document_url,
                        title=document_title,
                        body_text="",
                        status=row["status"],
                        content_type=content_type,
                        document_link_count=1,
                    )
                    self._core.execute(
                        """UPDATE pages SET title=?,page_kind=?,access_mode=?,value_score=?,
                           value_tier=?,score_reasons=?,published_at='',duplicate_of=NULL
                           WHERE url=?""",
                        (
                            document_title,
                            document_result.page_kind,
                            document_result.access_mode,
                            document_result.value_score,
                            document_result.value_tier,
                            json.dumps(document_result.score_reasons, ensure_ascii=False),
                            row["url"],
                        ),
                    )
                    keys = {row["url"], row["final_url"], row["canonical_url"]}
                    for key in filter(None, keys):
                        self._core.execute(
                            "DELETE FROM article_media WHERE article_url=?", (key,)
                        )
                        self._core.execute(
                            "UPDATE media SET article_url=NULL WHERE article_url=?", (key,)
                        )
                        removed += self._core.execute(
                            "DELETE FROM articles WHERE url=?", (key,)
                        ).rowcount
                    if scanned % 500 == 0:
                        self._core.commit()
                    continue
                if len(body) > max_reindex_bytes:
                    oversized_reason = json.dumps(["oversized_html"], ensure_ascii=False)
                    self._core.execute(
                        """UPDATE pages SET page_kind='oversized',access_mode='unknown',
                           value_score=0,value_tier='audit_only',score_reasons=?
                           WHERE url=?""",
                        (oversized_reason, row["url"]),
                    )
                    keys = {row["url"], row["final_url"], row["canonical_url"]}
                    for key in filter(None, keys):
                        self._core.execute("DELETE FROM article_media WHERE article_url=?", (key,))
                        self._core.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (key,))
                        removed += self._core.execute(
                            "DELETE FROM articles WHERE url=?", (key,)
                        ).rowcount
                    if scanned % 500 == 0:
                        self._core.commit()
                    continue
                content_type = row["content_type"] or ""
                html = _decode(body, {"content-type": content_type})
                page = extract_page(
                    row["final_url"] or row["url"],
                    html,
                    content_type or "text/html",
                    row["source_id"],
                )
            except (OSError, UnicodeError):
                continue
            duplicate_of = duplicate_first.get(sha256_bytes(body))
            if duplicate_of == row["url"]:
                duplicate_of = None
            result = score_page(
                url=row["url"],
                final_url=row["final_url"] or row["url"],
                title=page.title,
                body_text=page.article.body_text if page.article else "",
                html=html,
                status=row["status"],
                content_type=content_type or "text/html",
                has_article=page.article is not None,
                published_at=page.article.published_at if page.article else "",
                link_count=len(page.links),
                document_link_count=sum(1 for target in page.links if document_asset_url(target)),
                duplicate=bool(duplicate_of),
            )
            self._core.execute(
                """UPDATE pages SET page_kind=?,access_mode=?,value_score=?,value_tier=?,score_reasons=?,
                   published_at=?,duplicate_of=? WHERE url=?""",
                (
                    result.page_kind,
                    result.access_mode,
                    result.value_score,
                    result.value_tier,
                    json.dumps(result.score_reasons, ensure_ascii=False),
                    result.published_at,
                    duplicate_of,
                    row["url"],
                ),
            )
            for target in page.links:
                kind = "asset" if looks_like_asset(target) else "page"
                self._core.execute(
                    "INSERT OR IGNORE INTO links(source_url,target_url,source_id,kind,discovered_at) "
                    "VALUES(?,?,?,?,?)",
                    (row["url"], target, row["source_id"], kind, utc_now()),
                )
                # Reindexing is an offline metadata repair. Link discovery is
                # persisted above for audit, but only a crawl run may mutate
                # the fetch frontier.
            for target, published_at in page.link_dates.items():
                self.save_article_hint(target, published_at, row["url"])
            keys = {row["url"], row["final_url"], row["canonical_url"]}
            article = page.article
            if article and result.value_score >= 16 and not duplicate_of:
                cap = (source_caps or {}).get(str(row["source_id"]))
                if cap and cap > 0:
                    article.images = article.images[:cap]
                # Reindexing can change the article's image list. Remove old
                # relationships first so exports do not retain stale images.
                for key in filter(None, keys):
                    self._core.execute("DELETE FROM article_media WHERE article_url=?", (key,))
                    self._core.execute(
                        "UPDATE media SET article_url=NULL WHERE article_url=?", (key,)
                    )
                hint = ""
                for key in filter(None, keys):
                    hint = self.article_hint(key)
                    if hint:
                        break
                if hint and not article.published_at:
                    article.published_at = hint
                self.save_article(article)
                # Reindexing is intentionally offline.  Reattach any image
                # URLs that were already downloaded instead of silently
                # dropping the article_media relationships when the old
                # extraction is replaced.
                for image in article.images:
                    media_row = self._core.execute(
                        "SELECT local_path FROM media WHERE url=?", (image.url,)
                    ).fetchone()
                    if not media_row:
                        continue
                    self._core.execute(
                        """INSERT INTO article_media(article_url,image_url,local_path,alt,title,caption,created_at)
                           VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(article_url,image_url) DO UPDATE SET
                           local_path=excluded.local_path,alt=excluded.alt,title=excluded.title,
                           caption=excluded.caption""",
                        (
                            article.url,
                            image.url,
                            media_row["local_path"] or "",
                            image.alt,
                            image.title,
                            image.caption,
                            utc_now(),
                        ),
                    )
                articles += 1
            else:
                for key in filter(None, keys):
                    self._core.execute("DELETE FROM article_media WHERE article_url=?", (key,))
                    self._core.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (key,))
                    removed += self._core.execute("DELETE FROM articles WHERE url=?", (key,)).rowcount
            # Committing once per page turns this pass into millions of
            # synchronous SQLite fsyncs.  Keep the same transactionally
            # consistent result while amortizing the cost over small batches.
            if scanned % 500 == 0:
                self._core.commit()
        self._core.commit()
        # Older runs could have treated a non-HTML document URL as an article
        # when the origin returned an HTML error shell.  Keep those documents
        # in ``assets``/``pages`` but remove the misleading article records.
        document_rows = self._core.execute("SELECT url FROM articles")
        for row in document_rows:
            if not document_asset_url(row["url"]):
                continue
            url = row["url"]
            self._core.execute("DELETE FROM article_media WHERE article_url=?", (url,))
            self._core.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (url,))
            document_articles_removed += self._core.execute(
                "DELETE FROM articles WHERE url=?", (url,)
            ).rowcount
        if document_articles_removed:
            self._core.commit()
        return {
            "scanned": scanned,
            "articles": articles,
            "removed": removed,
            "document_articles_removed": document_articles_removed,
        }
