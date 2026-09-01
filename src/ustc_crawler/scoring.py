"""Small, explainable page-value and crawl-priority classifier.

The crawler deliberately keeps the classifier heuristic rather than pretending
that a score is a fact.  Every score is stored together with the reasons that
produced it, so a later audit can change the thresholds without losing the
underlying evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from .canonicalize import looks_like_uploaded_html_attachment
from .extract import parse_date

NEWS_RE = re.compile(
    r"(?:news|notice|announcement|press|bulletin|xwdt|tzgg|xwzx|xyxw|news_list|"
    r"新闻|公告|通知|动态|要闻|公示|媒体|/(?:info|article|show_)(?:[/_.?-]|$)|"
    r"/20\d{2}/\d{4}/c\d+a\d+/)",
    re.IGNORECASE,
)
RESOURCE_RE = re.compile(
    r"(?:assignment|homework|exercise|lab(?:work)?|exam|test|answer|solution|"
    r"syllabus|lecture|course[-_ ]?material|作业|习题|实验|报告|答案|参考材料|"
    r"试题|考试|讲义|课件|课程资源|优秀实验报告|作业区)",
    re.IGNORECASE,
)
AUTH_RE = re.compile(
    r"(?:login|logon|signin|sign[-_ ]?in|logout|password|用户名|密码|登录|认证|统一身份)",
    re.IGNORECASE,
)
NAV_RE = re.compile(
    r"(?:search|query|stats?|statistics|gradebook|submission|calendar|dashboard|"
    r"notification|logout|登录|搜索|查询|统计|成绩|提交记录|日历|仪表盘)",
    re.IGNORECASE,
)
DOCUMENT_RE = re.compile(
    r"\.(?:pdf|docx?|pptx?|ppsx?|xlsx?|csv|odt|ods|odp|rtf|wps|et|dps|caj|epub|tex|txt|md|pages|numbers|key)(?:$|[?#])",
    re.IGNORECASE,
)


@dataclass(slots=True)
class PageScore:
    page_kind: str = "page"
    access_mode: str = "public"
    value_score: int = 0
    value_tier: str = "not_indexed"
    score_reasons: list[str] = field(default_factory=list)
    published_at: str = ""


def _haystack(url: str, title: str = "", body_text: str = "") -> str:
    return " ".join((url or "", title or "", body_text[:8000] or "")).lower()


def _is_recent(value: str, now: datetime | None = None) -> bool:
    if not value:
        return False
    now = now or datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return now - timedelta(days=365) <= parsed <= now + timedelta(days=1)


def _is_within_days(value: str, days: int, now: datetime | None = None) -> bool:
    """Return whether an ISO-ish date is within the last N days."""

    if not value:
        return False
    now = now or datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return now - timedelta(days=days) <= parsed <= now + timedelta(days=1)


def _date_from_url(url: str) -> str:
    """Extract a publication date from common URL path patterns."""

    path = urlsplit(url).path
    match = re.search(r"/(20\d{2})/(\d{2})(\d{2})(?:/|$)", path)
    if match:
        return parse_date(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
    match = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", path)
    if match:
        return parse_date(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
    return ""


def is_obvious_low_value_url(url: str) -> tuple[bool, str]:
    """Return URL-only filters safe to apply before a request is made.

    These filters only remove URLs that are unambiguously interactive,
    tracking-heavy, or duplicate listing variants.
    """

    if not url:
        return True, "empty URL"
    parts = urlsplit(url)
    path = parts.path.lower()
    query = parts.query.lower()
    if looks_like_uploaded_html_attachment(url):
        return True, "uploaded HTML attachment"
    if path.endswith("/logout") or "/logout/" in path:
        return True, "logout endpoint"
    if path.endswith("/search") or "/search/" in path or "?search=" in query:
        return True, "search endpoint"
    if "/author/detail/" in path and any(
        key in query for key in ("pageindex=", "pagesize=", "showtype=", "orderby=")
    ):
        return True, "sorted/paginated author listing variant"
    if any(key in query for key in ("orderby=", "showtype=", "pagesize=")) and any(
        token in path for token in ("/list", "/search", "/detail")
    ):
        return True, "sorted/paginated duplicate variant"
    if any(token in path for token in ("gradebook", "submission_history", "/stats/", "/statistics/")):
        return True, "personal/statistics endpoint"
    if path.endswith("/webapps/login") or re.search(
        r"/(?:login|logon|signin|sign[-_]?in)(?:/|$)", path
    ):
        return True, "interactive login page"
    # Indico exposes calendar/print/author-management variants alongside the
    # public event and contribution pages.  They are duplicate UI shells or
    # authenticated workflows, and following them creates a large fan-out
    # without adding public event information.
    if "/event/" in path and (
        path.endswith("/event.ics")
        or "/manage/" in path
        or "/author/" in path
        or any(key in query for key in ("print=", "view=", "showtimezone="))
    ):
        return True, "duplicate Indico event variant"
    if "/category/" in path and (
        path.endswith(("/calendar", "/closest", "/upcoming", "/previous", "/statistics", "/search"))
        or ("/overview" in path and any(key in query for key in ("date=", "period=", "detail=")))
    ):
        return True, "duplicate Indico calendar variant"
    query_keys = {key.lower() for key, _value in parse_qsl(parts.query, keep_blank_values=True)}
    if query_keys & {"jsessionid", "sessionid", "sid", "token"}:
        return True, "session-specific URL"
    return False, ""


def url_priority(
    url: str, source_id: str = "", parent: str = "", published_at: str = ""
) -> int:
    """Return a queue priority; higher values are fetched first.

    Recent publication dates are boosted so that a resumed crawl fills recent
    content before older backlog.  The date may come from article hints, URL
    path patterns, or extraction metadata.
    """

    published_at = published_at or _date_from_url(url)
    document = document_asset_url(url)
    # Attachments discovered from a news page are useful, but the article page
    # itself should be fetched first.  Keep document URLs in a lower band so a
    # large attachment fan-out cannot starve publication pages.
    if document:
        haystack = _haystack(url, source_id)
        priority = 260
        if RESOURCE_RE.search(haystack):
            priority += 100
        if NEWS_RE.search(haystack):
            priority += 40
        if _is_within_days(published_at, 30):
            priority += 80
        elif _is_recent(published_at):
            priority += 40
        return min(priority, 500)

    haystack = _haystack(url, source_id)
    priority = 100
    if NEWS_RE.search(haystack):
        priority += 400
    if RESOURCE_RE.search(haystack):
        priority += 180
    elif parent and RESOURCE_RE.search(parent):
        priority += 80
    if "/sitemap" in url.lower() or url.lower().endswith((".rss", ".atom")):
        priority += 80
    if NAV_RE.search(haystack):
        priority -= 120
    if _is_within_days(published_at, 30):
        priority += 250
    elif _is_recent(published_at):
        priority += 120
    return min(priority, 900)


def score_page(
    *,
    url: str,
    final_url: str = "",
    title: str = "",
    body_text: str = "",
    html: str = "",
    status: int = 200,
    content_type: str = "text/html",
    has_article: bool = False,
    published_at: str = "",
    link_count: int = 0,
    document_link_count: int = 0,
    duplicate: bool = False,
    blocked_by_robots: bool = False,
) -> PageScore:
    """Classify one fetched response and explain its score.

    The score is an indexing recommendation, not an access-control decision.
    A low score still leaves the raw response and its URL in the audit trail;
    it only prevents boilerplate or duplicate content from becoming searchable.
    """

    target = final_url or url
    haystack = _haystack(f"{url} {target}", title, body_text)
    reasons: list[str] = []
    published = published_at or ""
    if blocked_by_robots:
        return PageScore("robots_blocked", "blocked", 0, "not_indexed", ["robots.txt blocked"], published)
    if status in {401, 403}:
        return PageScore("auth_gate", "auth_required", 0, "not_indexed", [f"HTTP {status} requires access"], published)
    if status <= 0 or status >= 400:
        return PageScore("error", "unavailable", 0, "not_indexed", [f"HTTP {status}"], published)

    if looks_like_uploaded_html_attachment(target):
        return PageScore(
            "document",
            "public",
            0,
            "not_indexed",
            ["uploaded HTML attachment"],
            "",
        )

    login_shell = bool(
        ("password" in html.lower() or 'type="password"' in html.lower())
        and (AUTH_RE.search(haystack) or "/webapps/login" in target.lower())
    )
    if login_shell:
        return PageScore("auth_gate", "login_required", 0, "not_indexed", ["login form without public body"], published)

    access_mode = "public"
    if duplicate:
        return PageScore(
            "duplicate",
            access_mode,
            3,
            "not_indexed",
            ["same response body already stored under another URL"],
            published,
        )

    news = bool(NEWS_RE.search(" ".join((url, target, title))))
    resource = bool(RESOURCE_RE.search(" ".join((url, target, title, body_text[:2500]))))
    navigation = bool(NAV_RE.search(" ".join((url, target, title))))
    body_length = len(body_text.strip())
    score = 0
    if news:
        score += 35
        reasons.append("news/notice publication signal")
    if resource:
        score += 30
        reasons.append("course/assignment/resource signal")
    if has_article:
        score += 20
        reasons.append("article metadata or detail-page structure")
    if published:
        if _is_recent(published):
            score += 20
            reasons.append("published within the last 365 days")
        else:
            score += 5
            reasons.append("publication date captured")
    if body_length >= 200:
        score += 10
        reasons.append("substantial text body")
    elif body_length < 80:
        score -= 20
        reasons.append("empty or near-empty body")
    if title:
        score += 5
        reasons.append("title captured")
    if document_link_count:
        score += 10
        reasons.append(f"{document_link_count} document attachment link(s)")
    score += 5
    reasons.append("direct public response")
    if navigation:
        score -= 20
        reasons.append("navigation/search/statistics boilerplate")
    if link_count > 0 and body_length and link_count > max(40, body_length // 12):
        score -= 10
        reasons.append("link-heavy listing with little unique text")
    if not has_article and not news and not resource and body_length < 160:
        score -= 10
        reasons.append("generic shell without a publication/resource signal")

    score = max(0, min(100, score))
    if score >= 70:
        tier = "full_index"
    elif score >= 40:
        tier = "metadata_and_assets"
    elif score >= 16:
        tier = "audit_only"
    else:
        tier = "not_indexed"
    is_document = document_asset_url(target) or (
        content_type
        and "html" not in content_type.lower()
        and "xhtml" not in content_type.lower()
        and "xml" not in content_type.lower()
    )
    if is_document:
        page_kind = "document"
    elif news and has_article:
        page_kind = "news_article"
    elif news:
        page_kind = "news_listing"
    elif resource:
        page_kind = "course_resource"
    elif has_article:
        page_kind = "article"
    elif navigation:
        page_kind = "navigation"
    elif body_length < 80:
        page_kind = "shell"
    else:
        page_kind = "page"
    return PageScore(page_kind, access_mode, score, tier, reasons, published)


def document_asset_url(url: str) -> bool:
    """Whether an asset URL is worth downloading as a document."""

    parts = urlsplit(url)
    path = parts.path.lower()
    if DOCUMENT_RE.search(path):
        return True
    # University CMS download handlers often hide the real extension behind
    # an id-only endpoint.  These query keys are stable public attachment
    # markers, unlike session/token parameters filtered elsewhere.
    if path.endswith(("/download", "/download/", "/download.jsp", "/download.aspx")):
        query = parts.query.lower()
        return any(key in query for key in ("attachment", "fileid", "wbfileid", "urltype", "download"))
    return False
