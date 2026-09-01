from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .canonicalize import host_matches, looks_like_asset, normalize_url
from .models import ArticleDocument, ImageRef, PageDocument, SourceConfig
from .scoring import url_priority


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def article_bundle_key(url: str) -> str:
    """Return the stable archive identity for one article URL."""
    return sha256_bytes(url.encode("utf-8", errors="replace"))


def article_bundle_path(data_dir: str | Path, url: str, suffix: str = ".json") -> Path:
    return Path(data_dir) / "articles" / f"{article_bundle_key(url)}{suffix}"


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path, data_dir: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir) if data_dir else self.db_path.parent
        for child in ("pages", "media", "articles", "assets", "exports"):
            (self.data_dir / child).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._schema()

    def close(self) -> None:
        self.db.close()

    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, organization_level TEXT NOT NULL,
              allowed_hosts TEXT NOT NULL, blocked_hosts TEXT NOT NULL DEFAULT '[]',
              seed_urls TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
              discovery_only INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS frontier (
              url TEXT PRIMARY KEY, source_id TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0,
              discovered_from TEXT, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, discovered_at TEXT NOT NULL, fetched_at TEXT,
              priority INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            CREATE TABLE IF NOT EXISTS pages (
              url TEXT PRIMARY KEY, source_id TEXT NOT NULL, final_url TEXT, status INTEGER NOT NULL,
              content_type TEXT, fetched_at TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0,
              discovered_from TEXT, sha256 TEXT, raw_path TEXT, title TEXT, canonical_url TEXT,
              error TEXT, blocked_by_robots INTEGER NOT NULL DEFAULT 0,
              page_kind TEXT NOT NULL DEFAULT 'unknown', access_mode TEXT NOT NULL DEFAULT 'unknown',
              value_score INTEGER NOT NULL DEFAULT 0, value_tier TEXT NOT NULL DEFAULT 'not_indexed',
              score_reasons TEXT NOT NULL DEFAULT '[]', published_at TEXT, duplicate_of TEXT,
              FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            CREATE INDEX IF NOT EXISTS pages_source_idx ON pages(source_id, fetched_at);
            CREATE TABLE IF NOT EXISTS links (
              source_url TEXT NOT NULL, target_url TEXT NOT NULL, source_id TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'page', discovered_at TEXT NOT NULL,
              PRIMARY KEY(source_url, target_url), FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            CREATE TABLE IF NOT EXISTS article_hints (
              url TEXT PRIMARY KEY, published_at TEXT, source_url TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS articles (
              url TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT, author TEXT,
              published_at TEXT, updated_at TEXT, category TEXT, summary TEXT, body_html TEXT,
              body_text TEXT, body_markdown TEXT, extraction_method TEXT, source_page_url TEXT,
              raw_json TEXT, content_hash TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
              FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            CREATE INDEX IF NOT EXISTS articles_date_idx ON articles(published_at);
            CREATE TABLE IF NOT EXISTS media (
              url TEXT PRIMARY KEY, article_url TEXT, source_page_url TEXT, local_path TEXT,
              mime_type TEXT, sha256 TEXT, size INTEGER NOT NULL DEFAULT 0, alt TEXT, title TEXT,
              caption TEXT, status TEXT NOT NULL, error TEXT, fetched_at TEXT,
              FOREIGN KEY(article_url) REFERENCES articles(url)
            );
            CREATE TABLE IF NOT EXISTS article_media (
              article_url TEXT NOT NULL, image_url TEXT NOT NULL, local_path TEXT,
              alt TEXT, title TEXT, caption TEXT, created_at TEXT NOT NULL,
              PRIMARY KEY(article_url, image_url), FOREIGN KEY(article_url) REFERENCES articles(url),
              FOREIGN KEY(image_url) REFERENCES media(url)
            );
            CREATE TABLE IF NOT EXISTS assets (
              url TEXT PRIMARY KEY, source_url TEXT NOT NULL, local_path TEXT, mime_type TEXT,
              sha256 TEXT, size INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, error TEXT,
              fetched_at TEXT, page_kind TEXT NOT NULL DEFAULT 'document', access_mode TEXT NOT NULL DEFAULT 'unknown',
              value_score INTEGER NOT NULL DEFAULT 0, score_reasons TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
              pages INTEGER NOT NULL DEFAULT 0, articles INTEGER NOT NULL DEFAULT 0,
              media INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS failures (
              id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, source_id TEXT NOT NULL,
              error TEXT NOT NULL, status INTEGER, attempts INTEGER NOT NULL DEFAULT 1,
              last_seen TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sources)")}
        if "blocked_hosts" not in columns:
            self.db.execute("ALTER TABLE sources ADD COLUMN blocked_hosts TEXT NOT NULL DEFAULT '[]'")
        frontier_columns = {row[1] for row in self.db.execute("PRAGMA table_info(frontier)")}
        if "priority" not in frontier_columns:
            self.db.execute("ALTER TABLE frontier ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        page_columns = {row[1] for row in self.db.execute("PRAGMA table_info(pages)")}
        page_migrations = {
            "page_kind": "TEXT NOT NULL DEFAULT 'unknown'",
            "access_mode": "TEXT NOT NULL DEFAULT 'unknown'",
            "value_score": "INTEGER NOT NULL DEFAULT 0",
            "value_tier": "TEXT NOT NULL DEFAULT 'not_indexed'",
            "score_reasons": "TEXT NOT NULL DEFAULT '[]'",
            "published_at": "TEXT",
            "duplicate_of": "TEXT",
        }
        for name, definition in page_migrations.items():
            if name not in page_columns:
                self.db.execute(f"ALTER TABLE pages ADD COLUMN {name} {definition}")
        asset_columns = {row[1] for row in self.db.execute("PRAGMA table_info(assets)")}
        asset_migrations = {
            "page_kind": "TEXT NOT NULL DEFAULT 'document'",
            "access_mode": "TEXT NOT NULL DEFAULT 'unknown'",
            "value_score": "INTEGER NOT NULL DEFAULT 0",
            "score_reasons": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in asset_migrations.items():
            if name not in asset_columns:
                self.db.execute(f"ALTER TABLE assets ADD COLUMN {name} {definition}")
        # Existing crawls predate priority ordering.  Backfill it once so a
        # resumed run immediately starts with guest bootstrap/news/resource
        # URLs instead of replaying the old depth-first order.
        for row in self.db.execute(
            "SELECT url,source_id,discovered_from FROM frontier WHERE priority=0"
        ).fetchall():
            self.db.execute(
                "UPDATE frontier SET priority=? WHERE url=?",
                (url_priority(row["url"], row["source_id"], row["discovered_from"] or ""), row["url"]),
            )
        self.db.execute("CREATE INDEX IF NOT EXISTS pages_value_idx ON pages(value_score DESC, fetched_at)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS frontier_priority_idx ON frontier(status, priority DESC, depth, discovered_at)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS frontier_pending_idx ON frontier(status, priority DESC, depth, discovered_at)"
        )
        self.db.commit()

    def add_source(self, source: SourceConfig) -> None:
        self.db.execute(
            """INSERT INTO sources(id,name,organization_level,allowed_hosts,blocked_hosts,seed_urls,aliases,discovery_only,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                 organization_level=excluded.organization_level, allowed_hosts=excluded.allowed_hosts,
                 blocked_hosts=excluded.blocked_hosts,
                 seed_urls=excluded.seed_urls, aliases=excluded.aliases, discovery_only=excluded.discovery_only""",
            (
                source.id,
                source.name,
                source.organization_level,
                json.dumps(source.allowed_hosts, ensure_ascii=False),
                json.dumps(source.blocked_hosts, ensure_ascii=False),
                json.dumps(source.seed_urls, ensure_ascii=False),
                json.dumps(source.aliases, ensure_ascii=False),
                int(source.discovery_only),
                utc_now(),
            ),
        )
        self.db.commit()

    def add_sources(self, sources: Iterable[SourceConfig]) -> None:
        for source in sources:
            self.add_source(source)

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
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO frontier(url,source_id,depth,discovered_from,status,discovered_at,priority)
               VALUES(?,?,?,?, 'pending', ?, ?)""",
            (url, source_id, depth, discovered_from, utc_now(), priority),
        )
        inserted = cursor.rowcount > 0
        if not inserted and revive_current:
            revived = self.db.execute(
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
            self.db.execute(
                "UPDATE frontier SET priority=MAX(priority, ?) WHERE url=? AND status='pending'",
                (priority, url),
            )
        self.db.commit()
        return inserted

    def enqueue_many(self, urls: Iterable[tuple[str, str, int, str]]) -> int:
        count = 0
        for url, source_id, depth, parent in urls:
            if self.enqueue(url, source_id, depth, parent):
                count += 1
        return count

    def reset_processing(self) -> None:
        self.db.execute("UPDATE frontier SET status='pending' WHERE status='processing'")
        self.db.commit()

    def reset_seeds_and_listings(self, source_ids: set[str]) -> int:
        """Re-enqueue seed URLs and news/course listing pages for incremental recrawl.

        Listing pages are the main source of newly published article links.  In
        incremental mode we revisit the shallow channel pages where new links
        appear, but do not replay every page of a deep historical archive.
        Article pages older than the source cutoff are still filtered during
        enqueue/process.
        """
        if not source_ids:
            return 0
        placeholders = ",".join("?" for _ in source_ids)
        params = tuple(sorted(source_ids))
        # A previously interrupted incremental run may have left thousands of
        # already-fetched archive pagination pages pending. Restore those deep
        # listings before resetting the current channel pages.
        self.db.execute(
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
        # Reset seeds and shallow listing/channel pages. Depth three covers a
        # seed -> section -> listing -> first pagination-page traversal.
        self.db.execute(
            f"""UPDATE frontier SET status='pending'
                WHERE source_id IN ({placeholders})
                  AND status IN ('done','error','filtered')
                  AND ((depth=0 AND trim(coalesce(discovered_from,''))='') OR url IN (
                      SELECT url FROM pages
                      WHERE source_id IN ({placeholders})
                        AND page_kind IN ('news_listing','course_resource','sitemap','feed')
                        AND depth <= 3
                  ))""",
            params + params,
        )
        self.db.commit()
        return self.db.total_changes

    def pending(self, source_id: str | None = None, limit: int = 0) -> list[sqlite3.Row]:
        clauses = ["status='pending'"]
        params: list[Any] = []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        limit_sql = " LIMIT ?" if limit else ""
        if limit:
            params.append(limit)
        return self.db.execute(
            f"SELECT * FROM frontier WHERE {' AND '.join(clauses)} ORDER BY priority DESC, depth, discovered_at{limit_sql}",
            params,
        ).fetchall()

    def mark_filtered(self, url: str, reason: str) -> None:
        self.db.execute(
            "UPDATE frontier SET status='filtered', last_error=?, fetched_at=? WHERE url=?",
            (reason, utc_now(), url),
        )
        self.db.commit()

    def filter_frontier(self, min_value_score: int = 16) -> dict[str, int]:
        """Batch-filter pending boilerplate and already-audited low-value URLs."""
        from .scoring import is_obvious_low_value_url, url_priority

        rows = self.db.execute(
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
            saved = self.db.execute(
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
            self.db.executemany(
                "UPDATE frontier SET status='filtered',last_error=?,fetched_at=? WHERE url=?",
                [(reason, utc_now(), url) for reason, url in filtered],
            )
        if priority_updates:
            self.db.executemany("UPDATE frontier SET priority=? WHERE url=?", priority_updates)
        self.db.commit()
        return {"checked": len(rows), "filtered": len(filtered), "reprioritized": len(priority_updates)}

    def mark_processing(self, url: str) -> None:
        self.db.execute(
            "UPDATE frontier SET status='processing', attempts=attempts+1 WHERE url=?", (url,)
        )
        self.db.commit()

    def mark_done(self, url: str, error: str = "") -> None:
        self.db.execute(
            "UPDATE frontier SET status=?, last_error=?, fetched_at=? WHERE url=?",
            ("error" if error else "done", error, utc_now(), url),
        )
        self.db.commit()

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
                self.db.execute("DELETE FROM article_media WHERE article_url=?", (old_url,))
                self.db.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (old_url,))
                self.db.execute("DELETE FROM articles WHERE url=?", (old_url,))
        self.db.execute(
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
        self.db.commit()
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
        row = self.db.execute(
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
        self.db.execute(
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
        self.db.commit()
        return local_path

    def update_page_score(self, url: str, score: Any) -> None:
        self.db.execute(
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
        self.db.commit()

    def save_links(self, source_url: str, source_id: str, links: Iterable[tuple[str, str]]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO links(source_url,target_url,source_id,kind,discovered_at) VALUES(?,?,?,?,?)",
            [(source_url, target, source_id, kind, utc_now()) for target, kind in links],
        )
        self.db.commit()

    def save_article_hint(self, url: str, published_at: str, source_url: str) -> None:
        self.db.execute(
            """INSERT INTO article_hints(url,published_at,source_url,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET published_at=excluded.published_at,
               source_url=excluded.source_url,updated_at=excluded.updated_at""",
            (url, published_at, source_url, utc_now()),
        )
        self.db.commit()

    def article_hint(self, url: str) -> str:
        row = self.db.execute(
            "SELECT published_at FROM article_hints WHERE url=?", (url,)
        ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def article_exists(self, url: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone()
            is not None
        )

    def article_alias_exists(self, url: str) -> bool:
        return (
            self.db.execute(
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
        page = self.db.execute(
            "SELECT access_mode,page_kind FROM pages WHERE url=?", (url,)
        ).fetchone()
        return page is None or (
            str(page["access_mode"] or "unknown") == "public"
            and str(page["page_kind"] or "unknown") in {"news_article", "article"}
        )

    def save_article(self, article: ArticleDocument) -> str:
        content_hash = sha256_bytes(article.body_text.encode("utf-8", errors="replace"))
        now = utc_now()
        self.db.execute(
            """INSERT INTO articles(url,source_id,title,author,published_at,updated_at,category,summary,body_html,
               body_text,body_markdown,extraction_method,source_page_url,raw_json,content_hash,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET source_id=excluded.source_id,title=excluded.title,
               author=excluded.author,published_at=excluded.published_at,updated_at=excluded.updated_at,
               category=excluded.category,summary=excluded.summary,body_html=excluded.body_html,
               body_text=excluded.body_text,body_markdown=excluded.body_markdown,
               extraction_method=excluded.extraction_method,source_page_url=excluded.source_page_url,
               raw_json=excluded.raw_json,content_hash=excluded.content_hash,last_seen=excluded.last_seen""",
            (
                article.url,
                article.source_id,
                article.title,
                article.author,
                article.published_at,
                article.updated_at,
                article.category,
                article.summary,
                article.body_html,
                article.body_text,
                article.body_markdown,
                article.extraction_method,
                article.source_page_url,
                json.dumps(article.raw_metadata, ensure_ascii=False),
                content_hash,
                now,
                now,
            ),
        )
        self.db.commit()
        self.write_article_bundle(article, content_hash)
        return content_hash

    def write_article_bundle(
        self, article: ArticleDocument, content_hash: str | None = None
    ) -> None:
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
        for row in self.db.execute(
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
        for row in self.db.execute("SELECT * FROM articles ORDER BY url"):
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
        self.db.execute(
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
        self.db.execute(
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
        self.db.commit()
        return local_path

    def link_media(self, image: ImageRef, article_url: str, source_page_url: str = "") -> None:
        """Attach an already stored media URL to another article reference."""
        row = self.db.execute(
            "SELECT local_path FROM media WHERE url=?", (image.url,)
        ).fetchone()
        if not row:
            return
        self.db.execute(
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
        self.db.commit()

    def _blocked_host_article_urls(
        self, source_id: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Return (url, source_id, blocked_host) for articles on blocked hosts."""
        params: list[str] = []
        where = ""
        if source_id:
            where = "WHERE id=?"
            params.append(source_id)
        rows = self.db.execute(
            f"SELECT id, blocked_hosts FROM sources {where}", params
        ).fetchall()
        results: list[tuple[str, str, str]] = []
        for source_row in rows:
            sid = str(source_row["id"])
            blocked = json.loads(source_row["blocked_hosts"] or "[]")
            if not blocked:
                continue
            article_rows = self.db.execute(
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
                self.db.execute("DELETE FROM article_media WHERE article_url=?", (url,))
                self.db.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (url,))
                removed += self.db.execute("DELETE FROM articles WHERE url=?", (url,)).rowcount
            self.db.commit()
        return {
            "candidate_urls": len(urls),
            "removed_articles": removed if commit else 0,
            "dry_run": not commit,
        }

    def _orphan_media_rows(self) -> list[sqlite3.Row]:
        """Return media rows with no article_media relationship."""
        return self.db.execute(
            """SELECT m.url, m.article_url, m.local_path, m.status
               FROM media m
               LEFT JOIN article_media am ON am.image_url=m.url
               WHERE am.image_url IS NULL"""
        ).fetchall()

    def _relink_orphan_media(self, rows: list[sqlite3.Row]) -> tuple[int, int]:
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
            article_row = self.db.execute(
                "SELECT 1 FROM articles WHERE url=?", (article_url,)
            ).fetchone()
            if not article_row:
                dangling += 1
                self.db.execute(
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

    def _orphan_media_breakdown(self, rows: list[sqlite3.Row]) -> dict[str, int]:
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
                for r in self.db.execute(
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
                self.db.execute("DELETE FROM media WHERE url=?", (row["url"],))
                deleted += 1
            self.db.commit()
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
        for row in self.db.execute("SELECT id, allowed_hosts FROM sources"):
            sid = str(row["id"])
            for host in json.loads(row["allowed_hosts"] or "[]"):
                host_to_source[str(host).lower().lstrip("*.")] = sid

        params: list[str] = []
        where = ""
        if source_id:
            where = "WHERE source_id=?"
            params.append(source_id)
        rows = self.db.execute(
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
                    self.db.execute(
                        "UPDATE articles SET source_id=? WHERE url=?", (matched, url)
                    )
                reassigned += 1
            elif not matched:
                if commit:
                    self.db.execute("DELETE FROM article_media WHERE article_url=?", (url,))
                    self.db.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (url,))
                    deleted += self.db.execute("DELETE FROM articles WHERE url=?", (url,)).rowcount
                else:
                    deleted += 1
        if commit and (reassigned or deleted):
            self.db.commit()
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
            rows = self.db.execute(
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
                to_delete = self.db.execute(
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
                        self.db.execute(
                            "DELETE FROM article_media WHERE article_url=? AND image_url=?",
                            (article_url, del_row["image_url"]),
                        )
                        self.db.execute(
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
            self.db.commit()
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
        self.db.execute(
            "INSERT INTO failures(url,source_id,error,status,last_seen) VALUES(?,?,?,?,?)",
            (url, source_id, error, status, utc_now()),
        )
        self.db.commit()

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
            name: int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for name, table in names.items()
        }
        result["frontier_filtered"] = int(
            self.db.execute("SELECT COUNT(*) FROM frontier WHERE status='filtered'").fetchone()[0]
        )
        result["pages_indexable"] = int(
            self.db.execute("SELECT COUNT(*) FROM pages WHERE value_score >= 16").fetchone()[0]
        )
        return result

    def export_jsonl(self, output: str | Path) -> Path:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.db.execute("SELECT * FROM articles ORDER BY published_at DESC, url").fetchall()
        with target.open("w", encoding="utf-8") as handle:
            for row in rows:
                item = dict(row)
                item["images"] = [
                    dict(image)
                    for image in self.db.execute(
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
        rows = self.db.execute(
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
        rows = self.db.execute(
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

        duplicate_rows = self.db.execute(
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
        rows = self.db.execute(page_query, page_params).fetchall()
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
                self.db.executemany(
                    """UPDATE pages SET page_kind=?,access_mode=?,value_score=?,value_tier=?,
                       score_reasons=?,published_at=?,duplicate_of=? WHERE url=?""",
                    updates,
                )
                self.db.commit()
                updates.clear()
        if updates:
            self.db.executemany(
                """UPDATE pages SET page_kind=?,access_mode=?,value_score=?,value_tier=?,
                   score_reasons=?,published_at=?,duplicate_of=? WHERE url=?""",
                updates,
            )
            self.db.commit()
        self.db.execute(
            """UPDATE assets SET value_score=(SELECT p.value_score FROM pages p WHERE p.url=assets.url),
               score_reasons=(SELECT p.score_reasons FROM pages p WHERE p.url=assets.url),
               access_mode=(SELECT p.access_mode FROM pages p WHERE p.url=assets.url)
               WHERE EXISTS (SELECT 1 FROM pages p WHERE p.url=assets.url)"""
        )
        self.db.commit()
        return {"scored": scored, "indexable": indexable, "duplicates": len(duplicate_rows)}

    def backfill_assets_from_pages(self) -> dict[str, int]:
        """Normalize legacy document responses into the assets inventory."""
        from .scoring import document_asset_url

        scanned = 0
        saved = 0
        for row in self.db.execute(
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
        for duplicate_row in self.db.execute(
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
        rows = self.db.execute(page_query, page_params)
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
                if len(body) > max_reindex_bytes:
                    oversized_reason = json.dumps(["oversized_html"], ensure_ascii=False)
                    self.db.execute(
                        """UPDATE pages SET page_kind='oversized',access_mode='unknown',
                           value_score=0,value_tier='audit_only',score_reasons=?
                           WHERE url=?""",
                        (oversized_reason, row["url"]),
                    )
                    keys = {row["url"], row["final_url"], row["canonical_url"]}
                    for key in filter(None, keys):
                        self.db.execute("DELETE FROM article_media WHERE article_url=?", (key,))
                        self.db.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (key,))
                        removed += self.db.execute(
                            "DELETE FROM articles WHERE url=?", (key,)
                        ).rowcount
                    if scanned % 500 == 0:
                        self.db.commit()
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
            self.db.execute(
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
                self.db.execute(
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
                    self.db.execute("DELETE FROM article_media WHERE article_url=?", (key,))
                    self.db.execute(
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
                    media_row = self.db.execute(
                        "SELECT local_path FROM media WHERE url=?", (image.url,)
                    ).fetchone()
                    if not media_row:
                        continue
                    self.db.execute(
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
                    self.db.execute("DELETE FROM article_media WHERE article_url=?", (key,))
                    self.db.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (key,))
                    removed += self.db.execute("DELETE FROM articles WHERE url=?", (key,)).rowcount
            # Committing once per page turns this pass into millions of
            # synchronous SQLite fsyncs.  Keep the same transactionally
            # consistent result while amortizing the cost over small batches.
            if scanned % 500 == 0:
                self.db.commit()
        self.db.commit()
        # Older runs could have treated a non-HTML document URL as an article
        # when the origin returned an HTML error shell.  Keep those documents
        # in ``assets``/``pages`` but remove the misleading article records.
        document_rows = self.db.execute("SELECT url FROM articles")
        for row in document_rows:
            if not document_asset_url(row["url"]):
                continue
            url = row["url"]
            self.db.execute("DELETE FROM article_media WHERE article_url=?", (url,))
            self.db.execute("UPDATE media SET article_url=NULL WHERE article_url=?", (url,))
            document_articles_removed += self.db.execute(
                "DELETE FROM articles WHERE url=?", (url,)
            ).rowcount
        if document_articles_removed:
            self.db.commit()
        return {
            "scanned": scanned,
            "articles": articles,
            "removed": removed,
            "document_articles_removed": document_articles_removed,
        }
