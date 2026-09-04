"""The single classifier shared by preview and ingestion.

Publication type is deliberately a small deterministic rule set.  Keeping
the Python and SQLite expressions in this module prevents the dashboard and
the sync client from silently assigning different types to the same article.
"""

from __future__ import annotations

from typing import Literal

PublicationType = Literal["news", "notice", "other"]
CLASSIFIER_VERSION = "publication-v3"


def _news_reportage_title(title: str) -> bool:
    """Return whether a title describes reportage rather than an announcement."""

    return "新闻" in title or "报道" in title


def _notice_signal(*values: str) -> bool:
    value = " ".join(values).lower()
    return (
        "/notice/" in value
        or "/announcement/" in value
        or "tzgg" in value
        or "xxgg" in value
        or "gonggao" in value
        or "tongzhi" in value
    )


def classify_publication(
    *,
    url: str,
    source_id: str = "",
    title: str = "",
    category: str = "",
    source_page_url: str = "",
    page_url: str = "",
    final_url: str = "",
    canonical_url: str = "",
    discovered_from: str = "",
    page_kind: str = "news_article",
) -> PublicationType:
    """Classify one extracted article using the persisted preview rules."""

    combined_url = " ".join(
        (url, source_page_url, page_url, final_url, canonical_url, discovered_from)
    ).lower()
    title_value = title or ""
    category_value = category or ""
    if (
        "/kecheng/video/" in combined_url
        or title_value.strip().casefold() == "faculty"
        or category_value.strip().casefold() in {"视频中心", "视频专访"}
        or (
            source_id == "unit-math-ustc-edu-cn"
            and category_value.strip().casefold()
            in {
                "fundamental mathematics",
                "computational mathematics",
                "probability theory and mathematical statistics",
                "applied mathematics",
                "mathematical physics",
            }
        )
    ):
        return "other"
    if _notice_signal(
        combined_url,
        # University CMS section ids are notices, while the article title may
        # contain no notice-specific wording at all.
        f"{source_id} {combined_url}" if source_id == "university" else "",
    ):
        return "notice"
    if source_id == "university" and any(
        token in combined_url
        for token in ("/info/1360/", "/info/1361/", "/info/1362/", "/info/1363/", "/info/1364/", "/info/1365/", "/info/1366/")
    ):
        return "notice"
    if source_id == "university" and "wbtreeid=136" in combined_url:
        return "notice"
    if any(token in category_value for token in ("通知", "公告", "公示")):
        return "notice"
    if any(token in title_value for token in ("公告", "公示")):
        return "notice"
    if (
        ("的通知" in title_value or title_value.endswith("通知") or "通知：" in title_value)
        and "通知书" not in title_value
    ):
        return "notice"
    if "时刻表" in title_value:
        return "notice"
    # Keep reportage titles in the news stream even when they mention an
    # admissions topic that would otherwise look administrative.
    if not _news_reportage_title(title_value) and "通告" in title_value:
        return "notice"
    if not _news_reportage_title(title_value) and "招生" in title_value and any(
        token in title_value
        for token in ("安排", "简章", "名单", "方案", "通告", "导师", "研究方向")
    ):
        return "notice"
    if not _news_reportage_title(title_value) and "复试" in title_value and any(
        token in title_value for token in ("办法", "流程", "规定", "录取")
    ):
        return "notice"
    if not _news_reportage_title(title_value) and "补充规定" in title_value:
        return "notice"
    if page_kind in {"course_resource", "document", "asset", "unknown"}:
        return "other"
    return "news"


def publication_type_sql(article_alias: str = "a", page_alias: str = "p") -> str:
    """Return the SQL equivalent of :func:`classify_publication`.

    Blank persisted values intentionally use the legacy signals.  That keeps
    manually imported pre-ORM rows visible until they are reindexed, while all
    newly saved articles carry an explicit classifier version and type.
    """

    url = (
        f"LOWER(COALESCE({article_alias}.url, '') || ' ' || "
        f"COALESCE({article_alias}.source_page_url, '') || ' ' || "
        f"COALESCE({page_alias}.url, '') || ' ' || COALESCE({page_alias}.final_url, '') || ' ' || "
        f"COALESCE({page_alias}.canonical_url, '') || ' ' || COALESCE({page_alias}.discovered_from, ''))"
    )
    title = f"COALESCE({article_alias}.title, '')"
    category = f"COALESCE({article_alias}.category, '')"
    fallback = f"""CASE
      WHEN {url} LIKE '%/kecheng/video/%'
        OR LOWER(TRIM({title})) = 'faculty'
        OR LOWER(TRIM({category})) IN ('视频中心', '视频专访')
        OR ({article_alias}.source_id = 'unit-math-ustc-edu-cn' AND LOWER(TRIM({category})) IN (
          'fundamental mathematics', 'computational mathematics',
          'probability theory and mathematical statistics', 'applied mathematics',
          'mathematical physics'
        ))
      THEN 'other'
      WHEN
        {url} LIKE '%/notice/%' OR {url} LIKE '%/announcement/%'
        OR {url} LIKE '%tzgg%' OR {url} LIKE '%xxgg%'
        OR {url} LIKE '%gonggao%' OR {url} LIKE '%tongzhi%'
        OR ({article_alias}.source_id='university' AND (
          {url} LIKE '%/info/1360/%' OR {url} LIKE '%/info/1361/%'
          OR {url} LIKE '%/info/1362/%' OR {url} LIKE '%/info/1363/%'
          OR {url} LIKE '%/info/1364/%' OR {url} LIKE '%/info/1365/%'
          OR {url} LIKE '%/info/1366/%' OR {url} GLOB '*wbtreeid=136[0-6]*'
        ))
        OR {category} LIKE '%通知%' OR {category} LIKE '%公告%' OR {category} LIKE '%公示%'
        OR {title} LIKE '%公告%' OR {title} LIKE '%公示%'
        OR (({title} LIKE '%的通知%' OR {title} LIKE '%通知' OR {title} LIKE '%通知：%')
            AND {title} NOT LIKE '%通知书%')
        OR {title} LIKE '%时刻表%'
        OR ({title} NOT LIKE '%新闻%' AND {title} NOT LIKE '%报道%' AND {title} LIKE '%通告%')
        OR ({title} NOT LIKE '%新闻%' AND {title} NOT LIKE '%报道%' AND {title} LIKE '%招生%' AND (
          {title} LIKE '%安排%' OR {title} LIKE '%简章%' OR {title} LIKE '%名单%'
          OR {title} LIKE '%方案%' OR {title} LIKE '%通告%'
          OR {title} LIKE '%导师%' OR {title} LIKE '%研究方向%'
        ))
        OR ({title} NOT LIKE '%新闻%' AND {title} NOT LIKE '%报道%' AND {title} LIKE '%复试%' AND (
          {title} LIKE '%办法%' OR {title} LIKE '%流程%' OR {title} LIKE '%规定%'
          OR {title} LIKE '%录取%'
        ))
        OR ({title} NOT LIKE '%新闻%' AND {title} NOT LIKE '%报道%' AND {title} LIKE '%补充规定%')
      THEN 'notice'
      WHEN COALESCE({page_alias}.page_kind, '') IN ('course_resource', 'document', 'asset', 'unknown')
      THEN 'other'
      ELSE 'news' END"""
    return (
        f"CASE WHEN COALESCE({article_alias}.publication_type, '') "
        f"IN ('news', 'notice', 'other') THEN {article_alias}.publication_type "
        f"ELSE {fallback} END"
    )


__all__ = ["CLASSIFIER_VERSION", "PublicationType", "classify_publication", "publication_type_sql"]
