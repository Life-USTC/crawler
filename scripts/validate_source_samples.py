#!/usr/bin/env python3
"""Strict, read-only archive verification with one deterministic sample per source."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ustc_crawler.config import load_config
from ustc_crawler.crawl import _decode
from ustc_crawler.discover import unit_sources
from ustc_crawler.extract import extract_page
from ustc_crawler.routing import source_id_for_url
from ustc_crawler.store import article_bundle_key, article_bundle_path

ARTICLE_FIELDS = (
    "url",
    "source_id",
    "title",
    "author",
    "published_at",
    "updated_at",
    "category",
    "summary",
    "body_text",
    "body_markdown",
    "extraction_method",
    "source_page_url",
)


def _path(data_dir: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    if candidate.parts and candidate.parts[0] == data_dir.name:
        return data_dir.parent / candidate
    return data_dir / candidate


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_file(db_path: Path) -> tuple[int, int]:
    return db_path.stat().st_size, db_path.stat().st_mtime_ns


def _record_error(result: dict[str, Any], code: str, detail: str) -> None:
    result["errors"].append({"code": code, "detail": detail})
    result["status"] = "fail"


def _page_for_article(
    connection: sqlite3.Connection, article: sqlite3.Row
) -> sqlite3.Row | None:
    aliases = tuple(
        dict.fromkeys(
            value
            for value in (article["source_page_url"], article["url"])
            if value
        )
    )
    if not aliases:
        return None
    placeholders = ",".join("?" for _ in aliases)
    return connection.execute(
        f"""
        SELECT * FROM pages
        WHERE url IN ({placeholders}) OR final_url IN ({placeholders})
           OR canonical_url IN ({placeholders})
        ORDER BY CASE WHEN url=? THEN 0 WHEN url=? THEN 1 ELSE 2 END,
                 fetched_at DESC, url LIMIT 1
        """,
        aliases * 3 + (article["source_page_url"] or "", article["url"] or ""),
    ).fetchone()


def _check_raw(
    result: dict[str, Any], page: sqlite3.Row, data_dir: Path
) -> bytes | None:
    result["checks"]["page"] = {
        "url": page["url"],
        "status": page["status"],
        "page_kind": page["page_kind"],
        "access_mode": page["access_mode"],
        "raw_path": page["raw_path"],
    }
    if not page["raw_path"]:
        _record_error(result, "raw_missing", "sample page has no raw_path")
        return None
    raw_path = _path(data_dir, str(page["raw_path"]))
    if not raw_path.is_file():
        _record_error(result, "raw_missing", str(raw_path))
        return None
    actual = _hash(raw_path)
    expected = str(page["sha256"] or "")
    if actual != expected:
        _record_error(result, "raw_hash_mismatch", f"db={expected} file={actual}")
    if raw_path.stem != expected:
        _record_error(result, "raw_filename_mismatch", f"stem={raw_path.stem} sha={expected}")
    result["checks"]["raw_archive"] = {
        "path": str(raw_path),
        "bytes": raw_path.stat().st_size,
        "sha256": actual,
    }
    return raw_path.read_bytes()


def _check_bundle(
    result: dict[str, Any], article: sqlite3.Row, data_dir: Path
) -> dict[str, Any] | None:
    json_path = article_bundle_path(data_dir, str(article["url"]))
    html_path = article_bundle_path(data_dir, str(article["url"]), ".html")
    result["checks"]["bundle"] = {"json": str(json_path), "html": str(html_path)}
    if not json_path.is_file() or not html_path.is_file():
        _record_error(result, "bundle_missing", f"json={json_path.is_file()} html={html_path.is_file()}")
        return None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _record_error(result, "bundle_invalid", str(error))
        return None
    expected_key = article_bundle_key(str(article["url"]))
    if payload.get("archive_key") != expected_key:
        _record_error(result, "bundle_archive_key_mismatch", str(payload.get("archive_key")))
    body_hash = hashlib.sha256(str(article["body_text"] or "").encode()).hexdigest()
    if body_hash != article["content_hash"] or payload.get("content_hash") != body_hash:
        _record_error(result, "body_hash_mismatch", f"computed={body_hash} db={article['content_hash']}")
    for field in ARTICLE_FIELDS:
        if payload.get(field, "") != (article[field] or ""):
            _record_error(result, "bundle_field_mismatch", field)
    try:
        raw_metadata = json.loads(str(article["raw_json"] or "{}"))
    except json.JSONDecodeError:
        raw_metadata = None
        _record_error(result, "article_raw_json_invalid", "raw_json is not JSON")
    if payload.get("raw_metadata") != raw_metadata:
        _record_error(result, "bundle_field_mismatch", "raw_metadata")
    if html_path.read_bytes().decode("utf-8") != (article["body_html"] or ""):
        _record_error(result, "bundle_html_mismatch", str(html_path))
    result["checks"]["bundle"]["content_hash"] = body_hash
    result["checks"]["bundle"]["images"] = len(payload.get("images") or [])
    return payload


def _check_reextract(
    result: dict[str, Any], article: sqlite3.Row, page: sqlite3.Row, raw: bytes
) -> None:
    text = _decode(raw, {"content-type": str(page["content_type"] or "")})
    parsed = extract_page(
        str(page["final_url"] or page["url"]),
        text,
        str(page["content_type"] or "text/html"),
        str(article["source_id"]),
    ).article
    if parsed is None:
        _record_error(result, "reextract_missing_article", "raw HTML no longer parses as an article")
        return
    mismatches = []
    for field in (
        "title",
        "author",
        "updated_at",
        "category",
        "summary",
        "body_html",
        "body_text",
        "body_markdown",
        "extraction_method",
    ):
        if getattr(parsed, field) != (article[field] or ""):
            mismatches.append(field)
    if parsed.published_at != (article["published_at"] or ""):
        hint = result["checks"].get("article_hint", "")
        if not (not parsed.published_at and hint == (article["published_at"] or "")):
            mismatches.append("published_at")
    for field in mismatches:
        _record_error(result, "reextract_mismatch", field)
    result["checks"]["reextract"] = {
        "title": parsed.title,
        "published_at": parsed.published_at,
        "fields_checked": 10,
        "mismatches": mismatches,
    }


def _check_media(
    result: dict[str, Any], connection: sqlite3.Connection, article_url: str, data_dir: Path
) -> None:
    rows = connection.execute(
        """
        SELECT am.image_url, am.local_path AS relation_path, m.*
        FROM article_media am LEFT JOIN media m ON m.url=am.image_url
        WHERE am.article_url=? ORDER BY am.image_url
        """,
        (article_url,),
    ).fetchall()
    checked = 0
    for row in rows:
        if row["url"] is None:
            _record_error(result, "media_orphan", str(row["image_url"]))
            continue
        if row["status"] != "ok":
            continue
        local = _path(data_dir, str(row["local_path"] or ""))
        if not local.is_file():
            _record_error(result, "media_file_missing", str(local))
            continue
        checked += 1
        if local.stat().st_size != row["size"] or _hash(local) != row["sha256"]:
            _record_error(result, "media_file_mismatch", str(local))
    result["checks"]["media"] = {"relations": len(rows), "files_verified": checked}


def validate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    db_path = Path(args.db).resolve()
    data_dir = Path(args.data_dir).resolve()
    output = Path(args.output).resolve()
    config_sources, _ = load_config(args.config, include_supplemental=True)
    sources = config_sources + unit_sources(args.units)
    expected = {source.id: source for source in sources}
    source_hosts = {
        source.id: (source.allowed_hosts, source.blocked_hosts) for source in sources
    }
    before = _snapshot_file(db_path)
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    data_version_before = int(connection.execute("PRAGMA data_version").fetchone()[0])
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "snapshot": {"db_path": str(db_path)},
        "source_inventory": {},
        "global_checks": [],
        "sources": [],
    }
    db_sources = {row["id"]: row for row in connection.execute("SELECT * FROM sources")}
    missing = sorted(expected.keys() - db_sources.keys())
    extra = sorted(db_sources.keys() - expected.keys())
    metadata_mismatches = []
    for source_id in sorted(expected.keys() & db_sources.keys()):
        source = expected[source_id]
        row = db_sources[source_id]
        actual = {
            "name": row["name"],
            "organization_level": row["organization_level"],
            "allowed_hosts": json.loads(row["allowed_hosts"]),
            "blocked_hosts": json.loads(row["blocked_hosts"]),
            "seed_urls": json.loads(row["seed_urls"]),
            "aliases": json.loads(row["aliases"]),
            "discovery_only": bool(row["discovery_only"]),
        }
        configured = {
            "name": source.name,
            "organization_level": source.organization_level,
            "allowed_hosts": source.allowed_hosts,
            "blocked_hosts": source.blocked_hosts,
            "seed_urls": source.seed_urls,
            "aliases": source.aliases,
            "discovery_only": source.discovery_only,
        }
        if actual != configured:
            metadata_mismatches.append(source_id)
    report["source_inventory"] = {
        "expected": len(expected),
        "database": len(db_sources),
        "missing": missing,
        "extra": extra,
        "metadata_mismatches": metadata_mismatches,
    }
    global_errors: list[dict[str, str]] = []
    if missing or extra:
        global_errors.append({"code": "source_inventory_mismatch", "detail": f"missing={missing} extra={extra}"})
    if metadata_mismatches:
        global_errors.append(
            {"code": "source_metadata_mismatch", "detail": str(metadata_mismatches)}
        )
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    frontier = dict(
        connection.execute("SELECT status, COUNT(*) FROM frontier GROUP BY status").fetchall()
    )
    if integrity != "ok":
        global_errors.append({"code": "integrity_check", "detail": str(integrity)})
    if foreign_keys:
        global_errors.append({"code": "foreign_key_check", "detail": str(foreign_keys[:10])})
    for status in ("pending", "processing"):
        if frontier.get(status, 0):
            global_errors.append({"code": f"frontier_{status}", "detail": str(frontier[status])})
    report["global_checks"] = {
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "frontier": frontier,
        "errors": global_errors,
    }

    for source_id in sorted(expected):
        result: dict[str, Any] = {
            "source_id": source_id,
            "source_name": expected[source_id].name,
            "sample_kind": "none",
            "sample_url": "",
            "status": "pass",
            "checks": {},
            "errors": [],
        }
        article = connection.execute(
            """
            SELECT * FROM articles WHERE source_id=?
            ORDER BY COALESCE(published_at, '') DESC, last_seen DESC, url LIMIT 1
            """,
            (source_id,),
        ).fetchone()
        page = _page_for_article(connection, article) if article else None
        if article and page and page["raw_path"]:
            result["sample_kind"] = "article"
            result["sample_url"] = article["url"]
            owner = source_id_for_url(str(article["url"]), source_hosts)
            result["checks"]["ownership"] = owner
            if owner != source_id:
                _record_error(result, "owner_mismatch", f"resolved={owner}")
            hint_row = connection.execute(
                "SELECT published_at FROM article_hints WHERE url IN (?,?) ORDER BY updated_at DESC LIMIT 1",
                (article["url"], article["source_page_url"]),
            ).fetchone()
            result["checks"]["article_hint"] = hint_row[0] if hint_row else ""
            raw = _check_raw(result, page, data_dir)
            _check_bundle(result, article, data_dir)
            _check_media(result, connection, str(article["url"]), data_dir)
            if raw is not None:
                _check_reextract(result, article, page, raw)
        else:
            page = connection.execute(
                """
                SELECT * FROM pages WHERE source_id=?
                ORDER BY CASE WHEN raw_path IS NOT NULL AND raw_path!='' THEN 0 ELSE 1 END,
                         COALESCE(published_at, '') DESC, fetched_at DESC, url LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            if page is None:
                _record_error(result, "no_sample", "source has no archived page")
            else:
                result["sample_kind"] = "page_fallback"
                result["sample_url"] = page["url"]
                owner = source_id_for_url(str(page["url"]), source_hosts)
                result["checks"]["ownership"] = owner
                if owner != source_id:
                    _record_error(result, "owner_mismatch", f"resolved={owner}")
                if page["raw_path"]:
                    _check_raw(result, page, data_dir)
                else:
                    result["checks"]["page"] = {
                        "url": page["url"],
                        "status": page["status"],
                        "page_kind": page["page_kind"],
                        "access_mode": page["access_mode"],
                        "error": page["error"],
                        "raw_path": "",
                    }
                    if (
                        int(page["status"] or 0) == 200
                        or not page["error"]
                        or page["access_mode"] not in {"blocked", "unavailable", "auth_required"}
                    ):
                        _record_error(
                            result,
                            "inaccessible_page_inconsistent",
                            "a page without raw HTML must record a non-200/blocked access failure",
                        )
        report["sources"].append(result)

    data_version_after = int(connection.execute("PRAGMA data_version").fetchone()[0])
    connection.rollback()
    connection.close()
    after = _snapshot_file(db_path)
    stable = before == after and data_version_before == data_version_after
    report["snapshot"]["stable"] = stable
    if not stable:
        global_errors.append(
            {
                "code": "snapshot_unstable",
                "detail": (
                    f"file_before={before} file_after={after} "
                    f"data_version_before={data_version_before} data_version_after={data_version_after}"
                ),
            }
        )
    counts = Counter(item["status"] for item in report["sources"])
    kinds = Counter(item["sample_kind"] for item in report["sources"])
    report["summary"] = {
        "required": len(expected),
        "sampled": len(expected) - kinds["none"],
        "passed": counts["pass"],
        "failed": counts["fail"],
        "article_samples": kinds["article"],
        "page_fallback_samples": kinds["page_fallback"],
        "global_errors": len(global_errors),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    exit_code = 0 if counts["fail"] == 0 and not global_errors and stable else 1
    return report, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/crawler.sqlite")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--config", default="config/sources.yaml")
    parser.add_argument("--units", default="data/discovered_units.json")
    parser.add_argument("--output", default="data/exports/source-sample-verification.json")
    args = parser.parse_args()
    report, exit_code = validate(args)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report: {Path(args.output).resolve()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
