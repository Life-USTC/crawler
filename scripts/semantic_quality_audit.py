#!/usr/bin/env python3
"""Compare saved publications and media with a fresh parse of archived HTML."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ustc_crawler.crawl import _decode
from ustc_crawler.extract import extract_page

SITE_TITLE_SUFFIX = re.compile(r"\s*[-－|｜]\s*中国科学技术大学\s*$")


def _archive_path(data_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == data_dir.name:
        return data_dir.parent / path
    return data_dir / path


def _shell_contaminated(body: str) -> bool:
    tail = body[-1000:]
    copyright_footer = "Copyright 中国科学技术大学" in tail and (
        "皖ICP备" in tail or "All Rights Reserved" in tail
    )
    navigation = all(marker in body for marker in ("科大新闻", "学校概况", "院系介绍"))
    return copyright_footer or navigation


def _sample(
    samples: dict[str, list[dict[str, Any]]],
    issue: str,
    value: dict[str, Any],
    maximum: int,
) -> None:
    if len(samples[issue]) < maximum:
        samples[issue].append(value)


def audit(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).resolve()
    data_dir = Path(args.data_dir).resolve()
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")

    inventory_query = "SELECT count(*) FROM articles"
    inventory_parameters: list[object] = []
    if args.source:
        placeholders = ",".join("?" for _ in args.source)
        inventory_query += f" WHERE source_id IN ({placeholders})"
        inventory_parameters.extend(args.source)
    articles_total = int(connection.execute(inventory_query, inventory_parameters).fetchone()[0])

    media_by_article: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute("SELECT article_url,image_url FROM article_media"):
        media_by_article[str(row["article_url"])].add(str(row["image_url"]))

    query = """
        SELECT a.url,a.source_id,a.source_page_url,a.title,a.author,a.published_at,
               a.updated_at,a.category,a.summary,a.body_text,
               p.url AS page_url,p.final_url,p.content_type,p.raw_path
        FROM articles a JOIN pages p ON p.url=a.source_page_url
    """
    parameters: list[object] = []
    if args.source:
        placeholders = ",".join("?" for _ in args.source)
        query += f" WHERE a.source_id IN ({placeholders})"
        parameters.extend(args.source)
    query += " ORDER BY a.url"
    if args.limit:
        query += " LIMIT ?"
        parameters.append(args.limit)

    counts: Counter[str] = Counter()
    counts["articles_total"] = articles_total
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        for row in connection.execute(query, parameters):
            counts["articles_scanned"] += 1
            common = {"url": row["url"], "source_id": row["source_id"]}
            title = str(row["title"] or "")
            body = str(row["body_text"] or "")
            if SITE_TITLE_SUFFIX.search(title):
                counts["stored_site_title_suffix"] += 1
                _sample(samples, "stored_site_title_suffix", common | {"title": title}, args.samples)
            if _shell_contaminated(body):
                counts["stored_page_shell"] += 1
                _sample(samples, "stored_page_shell", common, args.samples)

            raw_path = _archive_path(data_dir, str(row["raw_path"] or ""))
            if not raw_path.is_file():
                counts["raw_missing"] += 1
                _sample(samples, "raw_missing", common | {"path": str(raw_path)}, args.samples)
                continue
            try:
                raw = raw_path.read_bytes()
                html = _decode(raw, {"content-type": str(row["content_type"] or "")})
                parsed = extract_page(
                    str(row["final_url"] or row["page_url"]),
                    html,
                    str(row["content_type"] or "text/html"),
                    str(row["source_id"]),
                ).article
            except (OSError, UnicodeError, ValueError) as error:
                counts["reextract_error"] += 1
                _sample(samples, "reextract_error", common | {"error": str(error)}, args.samples)
                continue
            if parsed is None:
                counts["reextract_missing_article"] += 1
                _sample(samples, "reextract_missing_article", common, args.samples)
                continue

            changed = [
                field
                for field in (
                    "title",
                    "author",
                    "published_at",
                    "updated_at",
                    "category",
                    "summary",
                    "body_text",
                )
                if getattr(parsed, field) != str(row[field] or "")
            ]
            if changed:
                counts["stale_extraction"] += 1
                for field in changed:
                    counts[f"stale_{field}"] += 1
                _sample(samples, "stale_extraction", common | {"fields": changed}, args.samples)

            stored_media = media_by_article.get(str(row["url"]), set())
            parsed_media = {image.url for image in parsed.images}
            missing_media = sorted(parsed_media - stored_media)
            stale_media = sorted(stored_media - parsed_media)
            if missing_media:
                counts["missing_media_relations"] += 1
                counts["missing_media_objects"] += len(missing_media)
                _sample(
                    samples,
                    "missing_media_relations",
                    common | {"missing_count": len(missing_media), "images": missing_media[:5]},
                    args.samples,
                )
            if stale_media:
                counts["stale_media_relations"] += 1
                counts["stale_media_objects"] += len(stale_media)
                _sample(
                    samples,
                    "stale_media_relations",
                    common | {"stale_count": len(stale_media), "images": stale_media[:5]},
                    args.samples,
                )
    finally:
        connection.close()

    if not args.limit:
        counts["articles_without_exact_page"] = articles_total - counts["articles_scanned"]

    return {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database": str(db_path),
        "counts": dict(sorted(counts.items())),
        "samples": dict(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only semantic audit against archived source HTML"
    )
    parser.add_argument("--db", type=Path, default=Path("data/crawler.sqlite"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("data/exports/semantic-audit.json"))
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    report = audit(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
