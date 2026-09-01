from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path


def rows(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    return [dict(row) for row in conn.execute(query, params)]


def scalar(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> int:
    value = conn.execute(query, params).fetchone()[0]
    return int(value or 0)


def sample_articles(conn: sqlite3.Connection) -> list[dict[str, object]]:
    source_ids = [
        "news",
        "supplemental-oic",
        "supplemental-journal",
        "supplemental-gradunion",
        "supplemental-ef",
        "supplemental-pnp",
        "unit-iid-ustc-edu-cn",
    ]
    result: list[dict[str, object]] = []
    query = """
        SELECT a.url,a.source_id,a.title,a.author,a.published_at,
               length(trim(coalesce(a.body_text,''))) AS body_chars,
               count(am.image_url) AS image_count
        FROM articles a LEFT JOIN article_media am ON am.article_url=a.url
        WHERE a.source_id=?
          AND trim(coalesce(a.title,'')) <> ''
          AND length(trim(coalesce(a.body_text,''))) >= 200
          AND (a.published_at IS NULL OR a.published_at <= datetime('now'))
        GROUP BY a.url
        ORDER BY coalesce(a.published_at,'') DESC,a.url
        LIMIT 2
    """
    for source_id in source_ids:
        found = rows(conn, query, (source_id,))
        if found:
            result.extend(found)
    return result


def build_report(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        today = date.today().isoformat()
        cutoff = (date.today() - timedelta(days=365)).isoformat()
        counts = {
            table: scalar(conn, f"SELECT count(*) FROM {table}")
            for table in (
                "sources",
                "frontier",
                "pages",
                "articles",
                "media",
                "assets",
                "links",
                "failures",
                "article_media",
            )
        }
        missing = {
            field: scalar(
                conn,
                f"SELECT count(*) FROM articles WHERE {condition}",
            )
            for field, condition in {
                "title": "trim(coalesce(title,''))=''",
                "published_at": "trim(coalesce(published_at,''))=''",
                "author": "trim(coalesce(author,''))=''",
                "summary": "trim(coalesce(summary,''))=''",
                "body_text": "trim(coalesce(body_text,''))=''",
                "short_body_under_200_chars": "length(trim(coalesce(body_text,'')))<200",
            }.items()
        }
        recent = scalar(
            conn,
            """SELECT count(*) FROM articles
               WHERE substr(published_at,1,10) >= ?
                 AND substr(published_at,1,10) <= ?""",
            (cutoff, today),
        )
        future = scalar(
            conn,
            "SELECT count(*) FROM articles WHERE substr(published_at,1,10) > ?",
            (today,),
        )
        media_errors = rows(
            conn,
            """SELECT coalesce(nullif(error,''),'unknown') AS error,count(*) AS count
               FROM media WHERE status <> 'ok'
               GROUP BY error ORDER BY count DESC,error LIMIT 20""",
        )
        recent_sources = rows(
            conn,
            """WITH ac AS (
                     SELECT source_id,count(*) AS articles,
                     sum(CASE WHEN substr(published_at,1,10) >= ?
                                          AND substr(published_at,1,10) <= ? THEN 1 ELSE 0 END) AS recent
                     FROM articles GROUP BY source_id
                   ), pc AS (
                     SELECT source_id,count(*) AS pages FROM pages GROUP BY source_id
                   )
               SELECT s.id,s.name,s.discovery_only,
                      coalesce(ac.articles,0) AS articles,
                      coalesce(ac.recent,0) AS recent,
                      coalesce(pc.pages,0) AS pages
               FROM sources s
               LEFT JOIN ac ON ac.source_id=s.id
               LEFT JOIN pc ON pc.source_id=s.id
               ORDER BY recent DESC,articles DESC,s.id
               LIMIT 40""",
            (cutoff, today),
        )
        page_kinds = rows(
            conn,
            "SELECT page_kind,count(*) AS count FROM pages GROUP BY page_kind ORDER BY count DESC,page_kind",
        )
        value_tiers = rows(
            conn,
            "SELECT value_tier,count(*) AS count FROM pages GROUP BY value_tier ORDER BY count DESC,value_tier",
        )
        frontier_statuses = rows(
            conn,
            "SELECT status,count(*) AS count FROM frontier GROUP BY status ORDER BY status",
        )
        orphan_media = scalar(
            conn,
            """SELECT count(*) FROM article_media am
               LEFT JOIN articles a ON a.url=am.article_url
               WHERE a.url IS NULL""",
        )
        media_without_link = scalar(
            conn,
            """SELECT count(*) FROM media m
               LEFT JOIN article_media am ON am.image_url=m.url
               WHERE am.image_url IS NULL""",
        )
        duplicate_pages = scalar(
            conn,
            "SELECT count(*) FROM pages WHERE page_kind='duplicate' OR trim(coalesce(duplicate_of,''))<>''",
        )
        oversized_pages = rows(
            conn,
            """SELECT url,source_id,title,value_score,value_tier
               FROM pages WHERE page_kind='oversized'
               ORDER BY url LIMIT 20""",
        )
        return {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "inventory": counts,
            "frontier_statuses": frontier_statuses,
            "page_kinds": page_kinds,
            "value_tiers": value_tiers,
            "article_quality": {
                "recent_365_days": recent,
                "future_dated": future,
                "missing_fields": missing,
                "duplicate_pages": duplicate_pages,
            },
            "media_quality": {
                "status_errors": media_errors,
                "orphan_article_media": orphan_media,
                "media_without_article_link": media_without_link,
            },
            "oversized_pages": {
                "count": scalar(conn, "SELECT count(*) FROM pages WHERE page_kind='oversized'"),
                "samples": oversized_pages,
            },
            "recent_source_breadth": recent_sources,
            "sample_articles": sample_articles(conn),
            "processing_notes": [
                "News and audited supplemental sources were crawled with news-first priority.",
                "Oversized or unusually slow raw pages remain in the audit trail and are not indexed as articles.",
                "Media download failures are retained with their HTTP/connection error for later targeted retries.",
                "Legacy #Title/#Content, WordPress detail-title, and blank-heading fallbacks were applied in targeted re-extraction passes.",
            ],
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a read-only crawl quality report")
    parser.add_argument("--db", type=Path, default=Path("data/crawler.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("data/exports/quality-audit.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_report(args.db), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
