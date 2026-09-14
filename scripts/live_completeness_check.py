#!/usr/bin/env python3
"""Live completeness check for configured USTC sources."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import urllib.parse
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from ustc_crawler.canonicalize import normalize_url as canonical_url

DB_PATH = Path("data/crawler.sqlite")

DEFAULT_SOURCES = [
    ("news", "https://news.ustc.edu.cn/", "中国科大新闻网"),
    ("university", "https://www.ustc.edu.cn/", "中国科学技术大学"),
    ("supplemental-po", "https://po.ustc.edu.cn/main.htm", "党政办公室"),
    ("supplemental-sie", "https://sie.ustc.edu.cn/", "创新创业学院"),
    ("supplemental-pnp", "https://pnp.ustc.edu.cn/main.htm", "粒子物理与原子核物理学科"),
    ("unit-www-hfnl-ustc-edu-cn", "https://www.hfnl.ustc.edu.cn/26106/list.htm", "合肥微尺度物质科学国家研究中心"),
    ("unit-scms-ustc-edu-cn", "https://scms.ustc.edu.cn/17973/list.htm", "化学与材料科学学院"),
    ("unit-www-nsrl-ustc-edu-cn", "https://www.nsrl.ustc.edu.cn/", "国家同步辐射实验室"),
]


def configured_source_ids(sources_yaml: Path, units_json: Path | None = None) -> set[str]:
    """Return source ids resolvable from the curated config plus discovered units.

    Mirrors the id derivation in ustc_crawler.discover.unit_sources so a
    DEFAULT_SOURCES entry is valid only if crawl configuration can produce it.
    """
    raw = yaml.safe_load(sources_yaml.read_text(encoding="utf-8")) or {}
    ids = {str(item["id"]) for item in raw.get("sources", [])}
    if units_json is not None and units_json.exists():
        discovered = json.loads(units_json.read_text(encoding="utf-8"))
        for item in discovered.get("units", []):
            host = str(item.get("host", "")).lower()
            if host:
                ids.add("unit-" + re.sub(r"[^a-z0-9]+", "-", host).strip("-"))
    return ids


def fetch(url: str) -> str:
    cmd = [
        "curl", "-s", "-L", "-A",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "--max-time", "30", url,
    ]
    return subprocess.check_output(cmd, text=True, errors="ignore")


def extract_news_links(base_url: str, html: str, max_links: int = 10) -> list[str]:
    """Extract likely article links from USTC department pages."""
    seen: set[str] = set()
    links: list[str] = []
    article_path = re.compile(
        r"(?:^|/)(?:\d{4}/\d{4}/c\d+a\d+/page\.(?:htm|psp)|info/\d+/\d+\.htm)$",
        re.I,
    )
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        full = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlsplit(full)
        query = urllib.parse.parse_qs(parsed.query)
        is_notice = parsed.path.lower().endswith("/tzggcontent.jsp") and (
            query.get("urltype") == ["news.NewsContentUrl"]
        )
        if not article_path.search(parsed.path) and not is_notice:
            continue
        if full not in seen:
            seen.add(full)
            links.append(full)
        if len(links) >= max_links:
            break
    return links


def normalize_url(url: str) -> set[str]:
    """Return common normalizations of a URL for matching."""
    normalized = canonical_url(url)
    variants = {normalized}
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme == "https":
        variants.add(parsed._replace(scheme="http").geturl())
    else:
        variants.add(parsed._replace(scheme="https").geturl())
    return variants


def article_url_aliases(cur: sqlite3.Cursor) -> set[str]:
    """Return indexed article URLs plus duplicate/canonical page aliases."""
    rows = cur.execute(
        """
        SELECT url FROM articles
        UNION
        SELECT p.url FROM pages p JOIN articles a ON a.url=p.duplicate_of
        UNION
        SELECT p.url FROM pages p JOIN articles a ON a.url=p.canonical_url
        """
    ).fetchall()
    aliases: set[str] = set()
    for row in rows:
        aliases.update(normalize_url(str(row[0])))
    return aliases


def audited_unavailable_urls(cur: sqlite3.Cursor) -> dict[str, str]:
    rows = cur.execute(
        """SELECT url,access_mode FROM pages
           WHERE access_mode IN ('auth_required','login_required','blocked')"""
    ).fetchall()
    result: dict[str, str] = {}
    for url, access_mode in rows:
        for variant in normalize_url(str(url)):
            result[variant] = str(access_mode)
    return result


def check_source(
    article_urls: set[str],
    unavailable_urls: dict[str, str],
    source_id: str,
    base_url: str,
    name: str,
    limit: int,
) -> tuple[int, int, bool]:
    print(f"\n## {source_id} ({name})")
    print(f"Homepage: {base_url}")
    try:
        html = fetch(base_url)
    except subprocess.CalledProcessError as e:
        print(f"FETCH FAILED: {e}")
        return 0, 0, False

    links = extract_news_links(base_url, html, limit)
    if not links:
        print("ERROR: no news/notice links were extracted")
    print(f"First {len(links)} news/notice links extracted:")
    missing: list[str] = []
    covered = 0
    for i, link in enumerate(links, 1):
        variants = normalize_url(link)
        if variants & article_urls:
            status = "FOUND"
            covered += 1
        elif unavailable_modes := {unavailable_urls[url] for url in variants if url in unavailable_urls}:
            status = f"AUDITED:{sorted(unavailable_modes)[0]}"
            covered += 1
        else:
            status = "MISSING"
            missing.append(link)
        print(f"  {i}. [{status}] {link}")

    print(f"\nResult: {covered}/{len(links)} links covered")
    if missing:
        print("Missing URLs:")
        for m in missing:
            print(f"  - {m}")
    return covered, len(links), bool(links)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live completeness check for USTC sources")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--source", help="source ID to check")
    parser.add_argument("--url", help="homepage/listing URL to check")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum current article links to verify per source",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    article_urls = article_url_aliases(cur)
    unavailable_urls = audited_unavailable_urls(cur)

    if args.source and args.url:
        found, total, extracted = check_source(
            article_urls,
            unavailable_urls,
            args.source,
            args.url,
            args.source,
            args.limit,
        )
    elif args.source or args.url:
        print("error: --source and --url must be provided together")
        return 2
    else:
        print("# Live completeness check (priority news and notice sources)")
        print("-" * 80)
        found = 0
        total = 0
        extracted = True
        for src, base_url, name in DEFAULT_SOURCES:
            source_found, source_total, source_extracted = check_source(
                article_urls,
                unavailable_urls,
                src,
                base_url,
                name,
                args.limit,
            )
            found += source_found
            total += source_total
            extracted = extracted and source_extracted

    conn.close()
    return 0 if extracted and total and found == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
