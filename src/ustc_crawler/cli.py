from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .crawl import CrawlOptions, run_crawl
from .db import upgrade_database
from .discover import discover_units
from .media import MediaOptions, download_saved_images
from .store import Store
from .sync import (
    DEFAULT_BATCH_CONCURRENCY,
    DEFAULT_OBJECT_CONCURRENCY,
    MAX_BATCH_CONCURRENCY,
    MAX_OBJECT_CONCURRENCY,
    MAX_PUBLICATION_BATCH_ITEMS,
    IngestionSyncClient,
    SyncOptions,
    ingestion_secret_from_environment,
    sync_backfill,
)
from .web import serve_dashboard


def _path(value: str) -> str:
    return str(Path(value))


def _sync_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--server",
        default=os.environ.get("USTC_CRAWLER_SERVER", ""),
        help="server origin for publication ingestion",
    )


def _sync_storage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default="data/crawler.sqlite", type=_path)
    parser.add_argument("--data-dir", default="data", type=_path)


def _iso_date(value: str) -> str:
    from datetime import datetime

    if value == "":
        return value
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--since must be YYYY-MM-DD: {value}") from exc
    return value


def _source_image_caps(config_path: str) -> dict[str, int]:
    from .config import load_config

    sources, _ = load_config(config_path, include_supplemental=True)
    return {
        source.id: source.max_images_per_page
        for source in sources
        if source.max_images_per_page is not None and source.max_images_per_page > 0
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ustc-crawler", description="Crawl public USTC news and unit sites locally"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser(
        "discover-ustc", help="discover colleges/departments from the official directory"
    )
    discover.add_argument("--directory-url", default="https://www.ustc.edu.cn/yxjs.htm")
    discover.add_argument("--output", default="data/discovered_units.json", type=_path)

    crawl = sub.add_parser("crawl", help="crawl configured sources; rerun to resume")
    crawl.add_argument("--config", default="config/sources.yaml", type=_path)
    crawl.add_argument("--db", default="data/crawler.sqlite", type=_path)
    crawl.add_argument("--data-dir", default="data", type=_path)
    crawl.add_argument("--units", default="data/discovered_units.json", type=_path)
    crawl.add_argument("--include-units", action="store_true")
    crawl.add_argument(
        "--include-supplemental",
        action="store_true",
        help="include audited subdomain candidates outside the official directory",
    )
    crawl.add_argument(
        "--source",
        action="append",
        default=[],
        help="limit crawl to one or more source IDs (repeatable); default is all sources",
    )
    crawl.add_argument("--max-pages", type=int, default=0, help="0 means unlimited")
    crawl.add_argument("--max-pages-per-source", type=int, default=0, help="0 means unlimited")
    crawl.add_argument("--max-depth", type=int, default=0, help="0 means unlimited")
    crawl.add_argument("--concurrency", type=int, default=2)
    crawl.add_argument(
        "--delay", type=float, default=1.0, help="minimum seconds between requests to one host"
    )
    crawl.add_argument("--no-images", action="store_true")
    crawl.add_argument(
        "--max-images-per-page",
        type=int,
        default=30,
        help="0 means download every discovered article image",
    )
    crawl.add_argument("--max-image-bytes", type=int, default=20 * 1024 * 1024)
    crawl.add_argument(
        "--ignore-robots", action="store_true", help="only use with explicit site-owner permission"
    )
    crawl.add_argument(
        "--min-value-score",
        type=int,
        default=16,
        help="index/follow article content at or above this score; low-score pages remain in the audit trail",
    )
    crawl.add_argument(
        "--no-news-first",
        action="store_true",
        help="disable publication/news priority ordering (normally news is crawled first)",
    )
    crawl.add_argument(
        "--since",
        type=_iso_date,
        default="",
        help="skip articles and undiscovered pages whose publication date is earlier than YYYY-MM-DD",
    )
    crawl.add_argument(
        "--incremental",
        action="store_true",
        help="refresh shallow listings, recover unseen current links, and avoid replaying older archives",
    )

    stats = sub.add_parser("stats", help="show local crawl counts")
    stats.add_argument("--db", default="data/crawler.sqlite", type=_path)
    stats.add_argument("--data-dir", default="data", type=_path)

    export = sub.add_parser("export", help="export articles as UTF-8 JSONL")
    export.add_argument("--db", default="data/crawler.sqlite", type=_path)
    export.add_argument("--data-dir", default="data", type=_path)
    export.add_argument("--output", default="data/exports/articles.jsonl", type=_path)

    reindex = sub.add_parser("reindex", help="re-extract saved pages without network requests")
    reindex.add_argument("--config", default="config/sources.yaml", type=_path)
    reindex.add_argument("--db", default="data/crawler.sqlite", type=_path)
    reindex.add_argument("--data-dir", default="data", type=_path)
    reindex.add_argument(
        "--source",
        action="append",
        default=[],
        help="limit re-extraction to one or more source IDs (repeatable)",
    )

    retext = sub.add_parser(
        "retext",
        help="recompute article body_text from stored body_html without network requests",
    )
    retext.add_argument("--db", default="data/crawler.sqlite", type=_path)
    retext.add_argument("--data-dir", default="data", type=_path)
    retext.add_argument(
        "--source",
        action="append",
        default=[],
        help="limit recomputation to one or more source IDs (repeatable)",
    )

    rebuild_bundles = sub.add_parser(
        "rebuild-bundles", help="rebuild URL-specific article JSON/HTML archives"
    )
    rebuild_bundles.add_argument("--db", default="data/crawler.sqlite", type=_path)
    rebuild_bundles.add_argument("--data-dir", default="data", type=_path)

    score = sub.add_parser(
        "score-pages",
        help="quickly rescore the saved page inventory without reparsing HTML or making requests",
    )
    score.add_argument("--db", default="data/crawler.sqlite", type=_path)
    score.add_argument("--data-dir", default="data", type=_path)

    assets = sub.add_parser(
        "backfill-assets",
        help="copy legacy document responses already stored under pages into data/assets",
    )
    assets.add_argument("--db", default="data/crawler.sqlite", type=_path)
    assets.add_argument("--data-dir", default="data", type=_path)

    report = sub.add_parser("report", help="summarize publication activity per source")
    report.add_argument("--db", default="data/crawler.sqlite", type=_path)
    report.add_argument("--data-dir", default="data", type=_path)
    report.add_argument("--output", default="data/exports/source-report.json", type=_path)

    cleanup = sub.add_parser(
        "cleanup-data",
        help="remove misclassified blocked-host articles and orphan media (dry-run by default)",
    )
    cleanup.add_argument("--config", default="config/sources.yaml", type=_path)
    cleanup.add_argument("--db", default="data/crawler.sqlite", type=_path)
    cleanup.add_argument("--data-dir", default="data", type=_path)
    cleanup.add_argument(
        "--source",
        default="",
        help="limit blocked-host cleanup to one source (default: all sources)",
    )
    cleanup.add_argument(
        "--commit",
        action="store_true",
        help="actually delete rows; without this flag only counts are reported",
    )

    media = sub.add_parser(
        "download-images", help="download all images referenced by locally saved articles"
    )
    media.add_argument("--db", default="data/crawler.sqlite", type=_path)
    media.add_argument("--data-dir", default="data", type=_path)
    media.add_argument("--concurrency", type=int, default=16)
    media.add_argument(
        "--delay", type=float, default=0.5, help="minimum seconds between requests to one host"
    )
    media.add_argument("--max-image-bytes", type=int, default=20 * 1024 * 1024)
    media.add_argument(
        "--source",
        action="append",
        default=[],
        help="limit media downloads to one or more source IDs (repeatable)",
    )

    serve = sub.add_parser("serve", help="serve a read-only local crawl dashboard")
    serve.add_argument("--db", default="data/crawler.sqlite", type=_path)
    serve.add_argument("--data-dir", default="data", type=_path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)

    db_upgrade = sub.add_parser(
        "db-upgrade",
        help="apply pending Alembic migrations to an existing local database",
    )
    db_upgrade.add_argument("--db", default="data/crawler.sqlite", type=_path)

    sync = sub.add_parser(
        "sync",
        help=(
            "upload persisted publication batches using the machine ingestion secret "
            "(USTC_CRAWLER_INGESTION_SECRET)"
        ),
        description=(
            "Upload persisted publication batches. Set "
            "USTC_CRAWLER_INGESTION_SECRET in the process environment; "
            "the secret is never accepted as a command-line argument."
        ),
    )
    _sync_connection_args(sync)
    _sync_storage_args(sync)
    sync.add_argument(
        "--batch-size",
        type=int,
        default=50,
        metavar="N",
        help=f"items per ingestion request (default: 50, max: {MAX_PUBLICATION_BATCH_ITEMS})",
    )
    sync.add_argument("--max-payload-bytes", type=int, default=2 * 1024 * 1024)
    sync.add_argument("--max-batches", type=int, default=0, help="0 means all pending batches")
    sync.add_argument("--max-retries", type=int, default=3)
    sync.add_argument(
        "--object-concurrency",
        type=int,
        default=DEFAULT_OBJECT_CONCURRENCY,
        metavar="N",
        help=(
            "maximum concurrent object uploads/completions "
            f"(default: {DEFAULT_OBJECT_CONCURRENCY}, max: {MAX_OBJECT_CONCURRENCY})"
        ),
    )
    sync.add_argument(
        "--batch-concurrency",
        type=int,
        default=DEFAULT_BATCH_CONCURRENCY,
        metavar="N",
        help=(
            "maximum ingestion batches delivered concurrently "
            f"(default: {DEFAULT_BATCH_CONCURRENCY}, max: {MAX_BATCH_CONCURRENCY})"
        ),
    )

    sync_backfill_command = sub.add_parser(
        "sync-backfill",
        help="enqueue existing articles for later sync without making network requests",
    )
    _sync_storage_args(sync_backfill_command)
    sync_backfill_command.add_argument("--chunk-size", type=int, default=100)
    return parser


def _ingestion_server(args: argparse.Namespace) -> str:
    if not args.server:
        raise ValueError("--server is required for ingestion commands")
    return args.server


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sync":
        server = _ingestion_server(args)
        secret = ingestion_secret_from_environment()
        store = Store(args.db, args.data_dir)
        try:
            client = IngestionSyncClient(store.database, args.data_dir, server, secret)
            try:
                print(
                    json.dumps(
                        client.sync(
                            options=SyncOptions(
                                batch_size=args.batch_size,
                                max_payload_bytes=args.max_payload_bytes,
                                max_batches=args.max_batches,
                                max_retries=args.max_retries,
                                object_concurrency=args.object_concurrency,
                                batch_concurrency=args.batch_concurrency,
                            )
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            finally:
                client.close()
        finally:
            store.close()
        return 0
    if args.command == "sync-backfill":
        store = Store(args.db, args.data_dir)
        try:
            print(
                json.dumps(
                    sync_backfill(store, chunk_size=args.chunk_size),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            store.close()
        return 0
    if args.command == "discover-ustc":
        result = discover_units(args.directory_url, args.output)
        print(
            json.dumps(
                {
                    "units": len(result.get("units", [])),
                    "excluded": len(result.get("excluded", [])),
                    "error": result.get("error", ""),
                },
                ensure_ascii=False,
            )
        )
        return 0 if not result.get("error") else 1
    if args.command == "db-upgrade":
        upgrade_database(args.db)
        print(json.dumps({"database": args.db, "status": "upgraded"}, ensure_ascii=False))
        return 0
    if args.command == "crawl":
        options = CrawlOptions(
            config_path=args.config,
            db_path=args.db,
            data_dir=args.data_dir,
            units_path=args.units,
            include_units=args.include_units,
            include_supplemental=args.include_supplemental,
            max_pages=args.max_pages,
            max_pages_per_source=args.max_pages_per_source,
            max_depth=args.max_depth,
            concurrency=args.concurrency,
            delay=args.delay,
            download_images=not args.no_images,
            max_images_per_page=args.max_images_per_page,
            max_image_bytes=args.max_image_bytes,
            ignore_robots=args.ignore_robots,
            min_value_score=args.min_value_score,
            news_first=not args.no_news_first,
            since=args.since,
            incremental=args.incremental,
            source_ids=args.source,
        )
        print(json.dumps(run_crawl(options), ensure_ascii=False, indent=2))
        return 0
    if args.command == "stats":
        store = Store(args.db, args.data_dir)
        try:
            print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    if args.command == "export":
        store = Store(args.db, args.data_dir)
        try:
            print(store.export_jsonl(args.output))
        finally:
            store.close()
        return 0
    if args.command == "reindex":
        store = Store(args.db, args.data_dir)
        try:
            print(
                json.dumps(
                    store.reindex_extractions(
                        set(args.source) or None,
                        _source_image_caps(args.config),
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            store.close()
        return 0
    if args.command == "retext":
        store = Store(args.db, args.data_dir)
        try:
            print(
                json.dumps(
                    store.retext_article_bodies(set(args.source) or None),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            store.close()
        return 0
    if args.command == "rebuild-bundles":
        store = Store(args.db, args.data_dir)
        try:
            print(json.dumps(store.rebuild_article_bundles(), ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    if args.command == "score-pages":
        store = Store(args.db, args.data_dir)
        try:
            print(json.dumps(store.rescore_pages(), ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    if args.command == "backfill-assets":
        store = Store(args.db, args.data_dir)
        try:
            print(json.dumps(store.backfill_assets_from_pages(), ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    if args.command == "report":
        store = Store(args.db, args.data_dir)
        try:
            report = store.source_report(args.output)
            print(
                json.dumps(
                    {"sources": len(report), "output": args.output}, ensure_ascii=False, indent=2
                )
            )
        finally:
            store.close()
        return 0
    if args.command == "cleanup-data":
        source_caps = _source_image_caps(args.config)
        store = Store(args.db, args.data_dir)
        try:
            result = store.cleanup_data(
                source_id=args.source or None,
                commit=args.commit,
                source_caps=source_caps,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    if args.command == "download-images":
        result = download_saved_images(
            MediaOptions(
                db_path=args.db,
                data_dir=args.data_dir,
                concurrency=args.concurrency,
                delay=args.delay,
                max_image_bytes=args.max_image_bytes,
                source_ids=tuple(args.source),
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "serve":
        return serve_dashboard(args.db, args.data_dir, args.host, args.port)
    return 2
