from __future__ import annotations

import json
import re
import warnings
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning

from .adapters import adapter_for
from .canonicalize import normalize_url
from .markdown import html_to_markdown
from .models import ArticleDocument, ImageRef, PageDocument

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

DATE_PATTERNS = (
    re.compile(
        r"(?P<y>20\d{2})[年./-](?P<m>\d{1,2})[月./-](?P<d>\d{1,2})日?(?:[ T](?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?)?"
    ),
    re.compile(
        r"(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})(?:[ T](?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2}))?"
    ),
)
REMOVE_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}
CONTENT_SELECTORS = (
    "#Content",
    "#divContent",
    ".media-foucs",
    ".detail-content",
    ".wp_articlecontent",
    "#wp_articlecontent",
    "#SubPage",
    "#con_main",
    ".con_main",
    ".wl-detail",
    ".newsdetail_main",
    ".container_bg",
    ".central_text",
    ".newscont",
    ".inner-news-detail",
    ".wl-xueshu",
    ".cont .text",
    ".SubPage",
    ".news-detail-notes",
    ".articel-show-text",
    ".newscontent",
    ".newsNr",
    ".node__content",
    ".field--name-body",
    "article",
    "main",
    ".v_news_content",
    "#vsb_content",
    ".article-content",
    ".article_content",
    ".article-body",
    ".entry-content",
    ".post-content",
    ".page-content",
    ".confBodyBox",
    ".mainContent",
    ".conference-page",
    ".item-summary",
    ".contribution-description",
    ".contribution-content",
    ".paper-content",
    ".news_content",
    ".news-content",
    ".content",
    ".InfoBox",
    ".infobox",
    ".detail",
    ".article",
    "td.content",
    "td[class*='content']",
    "[class*='article']",
    "[class*='content']",
)
CONTENT_BOOSTS = {
    "#Content": 2200,
    "#divContent": 2200,
    ".media-foucs": 1800,
    ".detail-content": 1800,
    ".wp_articlecontent": 1800,
    "#wp_articlecontent": 1800,
    "#SubPage": 1500,
    "#con_main": 1600,
    ".con_main": 1600,
    ".wl-detail": 1800,
    ".newsdetail_main": 1800,
    ".container_bg": 1600,
    ".central_text": 1900,
    ".newscont": 1800,
    ".inner-news-detail": 1800,
    ".wl-xueshu": 1900,
    ".cont .text": 1800,
    ".SubPage": 1500,
    ".v_news_content": 2200,
    "#vsb_content": 2200,
    ".InfoBox": 1800,
    ".infobox": 1800,
    ".article-content": 850,
    ".article_content": 850,
    ".post-content": 800,
    ".page-content": 1800,
    ".confBodyBox": 1700,
    ".mainContent": 1600,
    ".conference-page": 1500,
    ".item-summary": 1500,
    ".contribution-description": 1900,
    ".contribution-content": 1900,
    ".paper-content": 1900,
    ".news-detail-notes": 1900,
    ".articel-show-text": 1800,
    ".newscontent": 1700,
    ".newsNr": 1700,
    ".node__content": 1600,
    ".field--name-body": 1600,
    "article": 700,
    "td.content": 500,
    "td[class*='content']": 450,
}
ARTICLE_PATH = re.compile(
    r"/(?:info|article|news|notice|notices|show_|view|detail|content)(?:[/?_.-]|$)", re.I
)
GENERIC_HEADINGS = {
    "首页",
    "新闻",
    "news",
    "news center",
    "news centre",
    "information",
    "information center",
    "新闻动态",
    "新闻速递",
    "热点新闻",
    "通知公告",
    "公告通知",
    "通知",
    "公告",
    "学院公告",
    "学术报告",
    "党建动态",
    "学工动态",
    "学院新闻",
    "综合新闻",
    "最新内容",
    "首页置顶",
    "最新消息",
    "位置栏目",
    "banner",
    "banner3",
    "banner信息",
    "影像",
    "faculty",
    "中国科学技术大学-研究生招生在线",
}

GENERIC_CATEGORIES = {
    "首页",
    "网站首页",
    "主页",
    "home",
    "index",
    "返回首页",
    "学院首页",
    "单位首页",
    "本站首页",
    "新闻",
    "news",
    "新闻中心",
    "news center",
    "news centre",
    "新闻动态",
    "新闻速递",
    "最新动态",
    "动态",
    "信息",
    "information",
    "info",
}

BREADCRUMB_SELECTORS = (
    ".breadcrumb",
    ".crumb",
    ".crumbs",
    ".navpath",
    ".location",
    ".n_position",
    ".breadcrumbs",
    ".breadCreamBar",
    ".inner-rposition",
    ".rpos-con",
    ".col_path",
    ".ert",
    "nav[aria-label='breadcrumb']",
    "nav[aria-label='Breadcrumb']",
    "[class*='breadcrumb']",
)


def _is_generic_heading(value: str) -> bool:
    """Return whether a heading is a section label rather than a post title."""
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return normalized in {item.casefold() for item in GENERIC_HEADINGS}


def _is_site_only_title(value: str) -> bool:
    """Return whether a document title names only the publishing site.

    A concrete ``<title>`` is normally safer than an arbitrary heading found
    inside the article body.  Keep the heading fallback for the small set of
    templates whose document title is only an institution or laboratory name.
    """

    normalized = re.sub(r"\s+", " ", value).strip(" -|｜").strip()
    if not normalized or _is_generic_heading(normalized):
        return True
    if len(normalized) <= 60 and re.fullmatch(
        r"(?:中国科学技术大学)?[^，。！？：:]{0,40}"
        r"(?:大学|学院|研究院|研究所|研究组|实验室|中心|新闻网|信息网|专题网|网站|官网)",
        normalized,
    ):
        return True
    return bool(
        len(normalized) <= 80
        and re.match(
            r"^(?:lab(?:oratory)?|school|college|department|institute|center|centre)\b",
            normalized,
            re.I,
        )
    )


def _is_generic_category(value: str) -> bool:
    """Return whether a breadcrumb/category segment is too generic to use."""
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return normalized in {item.casefold() for item in GENERIC_CATEGORIES}


def _looks_like_article_url(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path
    query = parts.query.lower()
    if "attachment_id=" in query:
        return False
    if any(token in path.lower() for token in ("/attachment/", "/download/")):
        return False
    if re.search(
        r"/(?:info|article|show_|view|detail|content)(?:[/?_.-]|$)"
        r"|/20\d{2}/\d{4}/c\d+a\d+/"
        r"|/web/news/\d+"
        r"|/news_detail-\d+"
        r"|/newslists/news/"
        r"|/class_\d+/(?:news|post)/",
        path,
        re.I,
    ):
        return True
    if re.search(r"/(?:notice|news)/[^/]+/\d+\.(?:html?|aspx)$", path, re.I):
        return True
    if re.search(r"/(?:notice|news)/\d+\.(?:html?|aspx)$", path, re.I):
        return True
    if path.lower().endswith("/tzggcontent.jsp") and "urltype=news.newscontenturl" in query:
        return True
    return "announceid=" in query or "itemid=" in query


def parse_date(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        while match:
            # Do not accept a partial match from an academic/business date
            # range such as ``2026.9-2027.1`` (which otherwise becomes
            # ``2026-09-20``).  A digit immediately after the match means the
            # regex stopped in the middle of a longer numeric token.
            after = text[match.end() : match.end() + 1]
            before = text[max(0, match.start() - 1) : match.start()]
            if not after.isdigit() and not before.isdigit():
                break
            match = pattern.search(text, match.start() + 1)
        if not match:
            continue
        groups = match.groupdict()
        try:
            date = datetime(
                int(groups["y"]),
                int(groups["m"]),
                int(groups["d"]),
                int(groups.get("h") or 0),
                int(groups.get("mi") or 0),
                int(groups.get("s") or 0),
            )
        except ValueError:
            continue
        return (
            date.isoformat(timespec="seconds")
            if any(groups.get(k) for k in ("h", "mi", "s"))
            else date.date().isoformat()
        )
    return ""


def _labeled_date(value: str) -> str:
    """Read a publication date only when the surrounding text labels it."""
    match = re.search(
        r"(?:发布时间|发布日期|发布于|发稿时间|更新时间|(?:来源|消息来源)\s*[:：]?\s*时间)"
        r"\s*[:：]?\s*([^\n|｜]{0,40})",
        value,
    )
    return parse_date(match.group(1)) if match else ""


def _trailing_body_date(value: str) -> str:
    """Read a date that appears unlabeled at the very end of the body text.

    Some USTC CMS templates (e.g. ``www.ustc.edu.cn``) place the publication
    date on the last line of the article without an explicit label. Only
    accept dates that are literally the final content so that event or
    deadline dates embedded elsewhere in the body are not mistaken for the
    publication time.
    """
    tail = value.strip()[-300:]
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*$", tail)
    if not match:
        return ""
    return parse_date(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return re.sub(r"\s+", " ", str(value)).strip()
    return ""


_BLOCK_TAGS = (
    "address", "article", "aside", "blockquote", "dd", "details", "div", "dl",
    "dt", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "ul",
)


def _block_text(root: Tag) -> str:
    """Plain text broken at block boundaries instead of every inline tag.

    ``get_text("\\n")`` explodes Word-exported pages that wrap each text run
    in its own inline ``<span>``; joining at block level keeps paragraphs and
    phone numbers on one line.
    """
    for br in root.find_all("br"):
        br.replace_with("\n")
    lines: list[str] = []
    for node in root.find_all(_BLOCK_TAGS):
        if node.find(_BLOCK_TAGS):
            continue
        text = re.sub(r"[^\S\n]+", " ", node.get_text(""))
        lines.extend(line.strip() for line in text.split("\n") if line.strip())
    if not lines:
        return re.sub(r"\n{3,}", "\n\n", root.get_text("\n", strip=True)).strip()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _jsonld_values(soup: BeautifulSoup) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for script in soup.select("script[type='application/ld+json']"):
        try:
            parsed = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        values = parsed if isinstance(parsed, list) else [parsed]
        for item in values:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                values.extend(x for x in item["@graph"] if isinstance(x, dict))
            elif isinstance(item, dict):
                result.append(item)
    return result


def _first_meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        element = soup.find("meta", attrs={"name": name}) or soup.find(
            "meta", attrs={"property": name}
        )
        if element and element.get("content"):
            return _text(element["content"])
    return ""


def _split_breadcrumb(text: str) -> list[str]:
    """Split breadcrumb text on common separators."""
    return [seg.strip() for seg in re.split(r"[>/»›\\|｜]", text) if seg.strip()]


def _category_from_breadcrumbs(soup: BeautifulSoup) -> str:
    """Extract the deepest meaningful segment from breadcrumb navigation."""
    for selector in BREADCRUMB_SELECTORS:
        node = soup.select_one(selector)
        if not node:
            continue
        segments: list[str] = []
        for child in node.find_all(["a", "span", "li"]):
            text = _text(child.get_text(" ", strip=True))
            if text:
                segments.append(text)
        if not segments:
            segments = _split_breadcrumb(_text(node.get_text(" ", strip=True)))
        meaningful = [seg for seg in segments if not _is_generic_category(seg)]
        if meaningful:
            return meaningful[-1]
    return ""


def _first_non_generic_section_heading(soup: BeautifulSoup, title: str) -> str:
    """Use the first section-label heading as a category hint.

    Section labels such as ``通知公告`` are too generic to be article titles,
    but they are exactly the kind of category label we want.  Skip only the
    most generic navigation labels and the article title itself.
    """
    for node in soup.find_all(["h1", "h2"]):
        text = _text(node.get_text(" ", strip=True))
        if text and text != title and not _is_generic_category(text):
            return text
    return ""


def _updated_at_from_time(soup: BeautifulSoup) -> str:
    """Find a <time> element that explicitly indicates modification time."""
    for time_node in soup.find_all("time"):
        datetime_attr = _text(time_node.get("datetime"))
        text = time_node.get_text(" ", strip=True)
        cls = " ".join(str(value) for value in time_node.get("class", []))
        node_id = str(time_node.get("id", ""))
        parent = time_node.find_parent()
        parent_text = parent.get_text(" ", strip=True) if parent else ""
        context = f"{cls} {node_id} {text} {parent_text}".casefold()
        modification_markers = ("update", "updated", "modified", "修改", "更新")
        if any(marker in context for marker in modification_markers):
            value = parse_date(datetime_attr) or parse_date(text)
            if value:
                return value
    return ""


def _author(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(filter(None, (_author(item) for item in value)))
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("alternateName"))
    return _text(value)


def _label_value(value: str, labels: str) -> str:
    if not value:
        return ""
    # CMS metadata is often rendered as adjacent spans and ``get_text`` then
    # flattens the whole bar (or, for a few templates, the whole article) into
    # one line.  Stop at the next metadata label instead of consuming it and
    # everything that follows as the current value.
    stop_labels = (
        r"作者|发布者|文章作者|来源|消息来源|发布时间|发布日期|发布于|"
        r"发稿时间|更新时间|修改时间|点击(?:量|次数)?|浏览(?:量|次数)?|访问(?:量|次数)?"
    )
    match = re.search(
        rf"(?:{labels})\s*[:：]\s*(.*?)"
        rf"(?=\s*(?:{stop_labels})\s*[:：]|[|｜\n]|$)",
        value,
    )
    return _text(match.group(1)) if match else ""


def _clean_signature_value(value: str) -> str:
    """Clean an extracted signature value and reject obvious non-authors."""
    value = _text(value)
    value = re.split(
        r"\s*(?:发布时间|发布日期|更新时间|修改时间|点击(?:量|次数)?|浏览(?:量|次数)?|访问(?:量|次数)?)\s*[:：]",
        value,
        maxsplit=1,
    )[0].strip()
    if not value or len(value) < 2 or len(value) > 120:
        return ""
    # Reject raw URLs that were mistaken for a source value.
    if value.lower().startswith(("http://", "https://", "www.")):
        return ""
    # Strip common trailing role markers (e.g. ``蒋瑜香/文`` or ``刘军喜摄``).
    value = re.sub(r"(?:[/／][文图摄编]|摄|摄影)$", "", value).strip()
    if len(value) < 2:
        return ""
    # Reject values that contain role words without a real name.
    if re.search(r"^(通讯员|编辑|记者|作者)$", value):
        return ""
    return value


def _trailing_publication_signature(body_text: str) -> str:
    """Extract reporter/source/editor signatures from the end of article body.

    Targets Chinese publication signatures commonly appended to reposted
    articles on ``news.ustc.edu.cn``, such as:

        记者：王敏 来源：中国科学报 2025-11-06 15:07
        来源：新华网
        文/张三
        编辑：李四
        （记者 黎静）

    Extraction is conservative: a clear label must be present, values stop at
    common delimiters, and only the trailing portion of the body is inspected.
    """
    if not body_text:
        return ""

    tail = body_text[-800:]
    # Characters that end a person/source name (quotes and brackets included).
    stop = r'（）()\[\]\n，,。；;|｜、："\'“”‘’「」『』《》\s'
    # Labels/dates/roles that terminate a preceding value.
    label_stop = r"(?:来源|记者|编辑|作者|文/|图片|摄影|摄像|日期|时间|http|www|原文|链接|发布|责任编辑|剪辑|审核|校对|素材|文章|通讯员|\d{4}|\d{1,2}:\d{2})"

    reporter = ""
    source = ""
    editor = ""

    # 1. Reporter with colon: 记者：XXX
    for match in re.finditer(
        rf"记者\s*[:：]\s*([^{stop}\d]+)(?=(?:{label_stop})|$|[{stop}])",
        tail,
    ):
        reporter = _clean_signature_value(match.group(1))

    # 2. Reporter without colon: 记者 XXX, 本报记者 XXX, 央视记者 XXX
    if not reporter:
        for match in re.finditer(
            rf"(?:本报|本网|本刊|本站|央视)?记者\s+([^{stop}\d]+)(?=(?:{label_stop})|$|[{stop}])",
            tail,
        ):
            reporter = _clean_signature_value(match.group(1))

    # 3. Author label: 作者：XXX
    if not reporter:
        for match in re.finditer(
            rf"作者\s*[:：]\s*([^{stop}\d]+)(?=(?:{label_stop})|$|[{stop}])",
            tail,
        ):
            reporter = _clean_signature_value(match.group(1))

    # 4. Writer label/slash: 文：XXX or 文/XXX
    if not reporter:
        for match in re.finditer(
            rf"文\s*[:：/]\s*([^{stop}\d]+)(?=(?:{label_stop})|$|[{stop}])",
            tail,
        ):
            reporter = _clean_signature_value(match.group(1))

    # 5. Editor: 编辑：XXX or 责任编辑：XXX
    if not reporter:
        for match in re.finditer(
            rf"(?:责任)?编辑\s*[:：]\s*([^{stop}\d]+)(?=(?:{label_stop})|$|[{stop}])",
            tail,
        ):
            editor = _clean_signature_value(match.group(1))

    # 6. Source: 来源：XXX (skip ``素材来源`` and ``文章来源``; prefer the last source)
    for match in re.finditer(
        rf"(?<![素材文章])来源\s*[:：]\s*([^{stop}\d]+)(?=(?:{label_stop})|$|[{stop}])",
        tail,
    ):
        source = _clean_signature_value(match.group(1))

    if reporter and source:
        return f"{reporter} / {source}"
    if reporter:
        return reporter
    if source:
        return source
    if editor:
        return editor

    # 7. Parenthesized organization at the very end: （人力资源部）, （生命科学与医学部）
    # Only accept it when the content looks like an organization/unit name.
    org_keywords = (
        "部|院|系|所|室|中心|实验室|办公室|委员会|工会|团委|党委|党组|"
        "报|社|网|台|刊|杂志|出版社|通讯社|媒体"
    )
    for match in re.finditer(
        r"[（(]([^)）]{2,30})[）)]\s*$",
        tail.strip(),
    ):
        value = _clean_signature_value(match.group(1))
        if value and re.search(org_keywords, value):
            return value

    return ""


def _title_from_document(
    soup: BeautifulSoup,
    current: str,
    url: str = "",
    *,
    current_is_heading: bool = False,
) -> str:
    """Prefer a detail heading over a generic section heading.

    Several USTC WordPress templates put the section name in the first h1 and
    the real post title in a second h1 under ``article``.  Their OpenGraph
    title can repeat the section name, so inspect article/main headings and the
    document title before accepting a generic value.  Indico event subpages
    put the event name in the first heading and the public subpage title after
    a colon in the document title.
    """

    candidates: list[str] = []
    for node in soup.select(
        "article h1, article h2, article h3.title, "
        "main h1, main h2, main h3.title, .bt01"
    ):
        value = _text(node.get_text(" ", strip=True))
        if value and value not in candidates:
            candidates.append(value)
    document_title = _text(soup.title.get_text(" ", strip=True) if soup.title else "")
    # The university homepage publishes its OpenGraph title as
    # ``文章标题-中国科学技术大学``.  That metadata is otherwise concrete, so it
    # used to return before the document-title cleanup below and leak the site
    # name into every saved article title.
    site_suffix = re.compile(r"\s*[-－|｜]+\s*中国科学技术大学\s*$")
    current = site_suffix.sub("", current).strip()
    document_title = site_suffix.sub("", document_title).strip()
    labeled_date_suffix = re.compile(
        r"\s*(?:发表日期|发布日期|发布时间|更新日期|更新时间)[：:]?\s*"
        r"20\d{2}[年./-]\d{1,2}[月./-]\d{1,2}日?"
        r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$"
    )
    current = labeled_date_suffix.sub("", current).strip()
    document_title = labeled_date_suffix.sub("", document_title).strip()
    if re.search(r"/event/\d+/(?:page|contributions)/", url, re.I):
        indico_title = document_title.split("·", 1)[0].strip()
        if ":" in indico_title:
            detail = _text(indico_title.rsplit(":", 1)[1])
            if detail and detail.casefold() != "overview":
                return detail
    if re.search(r"/event/\d+", url, re.I) and current and not _is_generic_heading(current):
        # Indico exposes the clean event name in OpenGraph metadata while its
        # document title appends dates, subpage labels, and product branding.
        return current
    if (
        current_is_heading
        and current
        and not _is_generic_heading(current)
        and not _is_site_only_title(current)
    ):
        # A detail heading is already more precise than a document title,
        # which often appends the institution name after a colon or dash.
        return current
    compact_suffix = re.fullmatch(
        r"(.{4,})\s*[-－|｜]+\s*(.{2,60})",
        document_title,
    )
    if compact_suffix and re.search(
        r"(?:大学|学院|研究院|研究所|实验室|新闻中心|新闻网|信息网|专题网|"
        r"共享中心|办公室|委员会|主题教育|学习教育|"
        r"中国科学技术大学.{0,30}网|中国科大.{0,30}网)$",
        compact_suffix.group(2),
    ):
        prefix = _text(compact_suffix.group(1))
        if prefix and not _is_generic_heading(prefix):
            return prefix
    for value in reversed(candidates):
        if (
            not _is_generic_heading(value)
            and document_title.startswith(value)
            and document_title[len(value) :].lstrip().startswith((":", "：", "-", "－", "|", "｜"))
        ):
            return value
    if document_title and not _is_site_only_title(document_title):
        # Do this before considering generic ``article/main h2`` candidates.
        # Rich-text editors frequently put paragraphs inside h2 elements; the
        # old reverse-candidate search then promoted body prose to the title.
        return document_title
    for value in reversed(candidates):
        if not _is_generic_heading(value) and len(value) >= 4:
            return value
    if document_title:
        # Common separators distinguish the post title from the site name.
        for separator in (" | ", " - ", "｜"):
            if separator in document_title:
                value = _text(document_title.split(separator, 1)[0])
                if value and not _is_generic_heading(value):
                    return value
        if current and not _is_generic_heading(current) and not _is_site_only_title(current):
            return current
        # When the document title matches a concrete heading candidate (e.g. an
        # <h3 class="title"> inside the article), it is the real post title even
        # if it contains site/department words such as ``学院``.
        if document_title in candidates:
            return document_title
        site_markers = (
            "ustc",
            "university",
            "institute",
            "college",
            "school",
            "laboratory",
            "lab for",
            "中国科学技术大学",
            "学院",
            "研究院",
            "实验室",
        )
        if any(marker in document_title.casefold() for marker in site_markers):
            for node in soup.select("article p, main p, .entry-content p"):
                value = _text(node.get_text(" ", strip=True))
                if len(value) >= 20 and not _is_generic_heading(value):
                    return value[:160].rstrip(" ,;，；")
        if not _is_generic_heading(document_title):
            return document_title
    # A few lightweight WordPress-style sites omit the post title entirely
    # and leave only a generic ``News`` heading.  The lead paragraph is still
    # a better searchable label than the section name in that case.
    for node in soup.select("article p, main p, .entry-content p"):
        value = _text(node.get_text(" ", strip=True))
        if len(value) >= 20 and not _is_generic_heading(value):
            return value[:160].rstrip(" ,;，；")
    return current


def _date_from_url(url: str) -> str:
    path = urlsplit(url).path
    match = re.search(r"/(20\d{2})/(\d{2})(\d{2})(?:/|$)", path)
    if match:
        return parse_date(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
    match = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", path)
    if not match:
        return ""
    return parse_date(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")


def _is_hidden(node: Tag) -> bool:
    """Identify hidden widgets that should not become article content."""
    if node.has_attr("hidden") or str(node.get("aria-hidden", "")).lower() == "true":
        return True
    style = re.sub(r"\s+", "", _text(node.get("style"))).casefold()
    if re.search(r"(?:^|;)display:none(?:;|$)", style) or re.search(
        r"(?:^|;)visibility:hidden(?:;|$)", style
    ):
        return True
    marker = " ".join(
        [str(node.get("id", "")), " ".join(str(value) for value in node.get("class", []))]
    ).casefold()
    return "tz-selector" in marker or "timezone-selector" in marker


def _is_shell_container(node: Tag) -> bool:
    if node.name in {"header", "footer", "nav", "aside"}:
        return True
    markers = [str(node.get("id", "")), *(str(value) for value in node.get("class", []))]
    if any(
        marker.strip().casefold()
        in {
            "header",
            "mainnav",
            "nnav",
            "navpull",
            "mainleft",
            "ntit",
            "local",
            "infotit",
            "arti_metas",
            "arti_publisher",
            "arti_update",
            "arti_views",
            "newsbtn",
            "wpartfuns",
            "wp_artfuns",
            "wp_art_adjoin",
        }
        for marker in markers
    ):
        return True
    return any(
        re.search(
            r"(?:^|[-_])(?:foot(?:er)?|bottom|copyright|friendlinks?|friendlylinks?)(?:$|[-_])",
            marker,
            re.IGNORECASE,
        )
        for marker in markers
    )


def _content_root(soup: BeautifulSoup) -> Tag:
    candidates: list[tuple[int, Tag]] = []
    for selector in CONTENT_SELECTORS:
        for node in soup.select(selector):
            # Some legacy CMS templates keep the article markup in a hidden
            # ``#divContent`` node and reveal it with JavaScript after load.
            # It is still the authoritative server-rendered article body.
            hidden_content = selector == "#divContent"
            if (not hidden_content and _is_hidden(node)) or any(
                _is_hidden(parent) for parent in node.parents
            ):
                continue
            if _is_shell_container(node) or any(
                _is_shell_container(parent) for parent in node.parents if isinstance(parent, Tag)
            ):
                continue
            clone_text = node.get_text(" ", strip=True)
            # Timetables, profiles, and similar pages can legitimately have a
            # short or image-only body. A strongly named article container is
            # still authoritative; discarding it makes the <body> fallback
            # import navigation, footer text, and decorative site images.
            has_image = any(
                image.get("data-src")
                or image.get("data-original")
                or image.get("data-lazy-src")
                or image.get("src")
                or image.get("srcset")
                for image in node.find_all("img")
            )
            has_embedded_document = bool(
                node.select_one("[pdfsrc], [swsrc], [vurl], [sudy-wp-src], video[src], audio[src], source[src]")
            ) or any(
                "showVsb" in script.get_text(" ", strip=True)
                or "vsb_pdf_image_data" in script.get_text(" ", strip=True)
                for script in node.find_all("script")
            )
            authoritative_content = (
                CONTENT_BOOSTS.get(selector, 0) >= 1500
                or node.name == "article"
                or (selector in CONTENT_BOOSTS and has_image)
            ) and (
                len(clone_text) >= 2
                or has_image
                or has_embedded_document
                or selector == ".cont .text"
                or selector == ".InfoBox"
                or selector == ".infobox"
                or selector == ".newsNr"
            )
            if len(clone_text) < 40 and not authoritative_content:
                continue
            links = len(node.select("a"))
            score = len(clone_text) - min(links * 20, len(clone_text) // 2)
            score += CONTENT_BOOSTS.get(selector, 0)
            if node.name == "article":
                score += 250
            if node.name == "main":
                score += 100
            candidates.append((score, node))
    # A malformed legacy VisualSiteBuilder template closes its nominal
    # ``td.content`` before emitting the real article in the following table
    # row.  Recognize that exact empty-placeholder shape so the generic body
    # fallback does not absorb the site's navigation and footer.
    for placeholder in soup.select("td.content"):
        if placeholder.get_text(" ", strip=True) or not placeholder.select_one(
            "#vsb_content, .v_news_content, .wp_articlecontent"
        ):
            continue
        row = placeholder.find_parent("tr")
        sibling = row.find_next_sibling("tr") if row else None
        content_cell = sibling.find("td") if sibling else None
        if content_cell and len(content_cell.get_text(" ", strip=True)) >= 40:
            candidates.append((2100 + len(content_cell.get_text(" ", strip=True)), content_cell))
    # The oldest graduate-admissions template wraps the title and article in
    # a plain table whose only stable marker is the ``td.bt01`` title cell.
    for title_cell in soup.select("td.bt01"):
        table = title_cell.find_parent("table")
        if table and len(table.get_text(" ", strip=True)) >= 40:
            candidates.append((2050 + len(table.get_text(" ", strip=True)), table))
    if candidates:
        return max(candidates, key=lambda pair: pair[0])[1]
    return soup.body or soup


def _clean_root(root: Tag) -> None:
    for node in reversed(root.find_all(True)):
        if _is_hidden(node):
            node.decompose()
    for node in root.find_all(REMOVE_TAGS):
        node.decompose()
    for node in root.find_all(["header", "footer", "nav", "aside"]):
        node.decompose()
    for node in root.select(", ".join(BREADCRUMB_SELECTORS)):
        node.decompose()
    # A few table-era templates put an unclassified breadcrumb table inside
    # the otherwise correct article container. Restrict this fallback to a
    # compact, media-free table so real tabular article content is preserved.
    for node in root.find_all(["table", "tr", "div", "span", "p"]):
        value = _text(node.get_text(" ", strip=True))
        if (
            len(value) <= 120
            and re.match(r"^(?:您的当前位置|当前位置|您现在的位置)\s*[:：]?", value)
            and not node.select_one("img, [pdfsrc], [swsrc]")
        ):
            node.decompose()
    # Broken legacy table markup can make BeautifulSoup nest the site's footer
    # inside an otherwise authoritative article container. Remove a compact
    # block carrying the filing/copyright signature before extracting text and
    # images; class names on these old footers are not consistent.
    for node in reversed(root.find_all(["div", "td", "section"])):
        value = _text(node.get_text(" ", strip=True))
        if len(value) <= 600 and (
            "皖ICP备" in value
            or (
                "Copyright 中国科学技术大学" in value
                and "All Rights Reserved" in value
            )
        ):
            node.decompose()
    for node in reversed(root.find_all(True)):
        if _is_shell_container(node):
            node.decompose()


def _remove_repeated_title(root: Tag, title: str) -> None:
    """Remove a title node already rendered separately by the detail page."""

    if not title:
        return
    for node in root.select(
        "h1, h2, h3, h4, h5, .article-title, .arti_title, .post-title, "
        ".entry-title, .detail_title, .newstitle, .wl-detail-title, td.bt01, .center_titlea"
    ):
        if node is not root and _text(node.get_text(" ", strip=True)) == title:
            node.decompose()


def _image_refs(root: Tag, page_url: str) -> list[ImageRef]:
    result: list[ImageRef] = []
    seen: set[str] = set()
    for image in root.find_all("img"):
        raw = (
            image.get("data-src")
            or image.get("data-original")
            or image.get("data-lazy-src")
            or image.get("src")
        )
        raw = raw or (
            image.get("srcset", "").split(",", 1)[0].strip().split(" ", 1)[0]
            if image.get("srcset")
            else ""
        )
        url = normalize_url(raw, page_url)
        if not url or url in seen:
            continue
        seen.add(url)
        caption = ""
        parent = image.find_parent("figure")
        if parent:
            caption = _text(
                parent.find("figcaption").get_text(" ", strip=True)
                if parent.find("figcaption")
                else ""
            )
        result.append(
            ImageRef(
                url=url,
                alt=_text(image.get("alt")),
                title=_text(image.get("title")),
                caption=caption,
            )
        )
    return result


def _visual_sitebuilder_player_urls(soup: BeautifulSoup, page_url: str) -> tuple[list[str], list[str]]:
    """Extract media hidden in VisualSiteBuilder video/PDF player scripts."""

    urls: list[str] = []
    images: list[str] = []
    seen: set[str] = set()
    for script in soup.find_all("script"):
        value = script.get_text(" ", strip=True)
        if "showVsb" not in value and "vsb_pdf_image_data" not in value:
            continue
        for match in re.finditer(
            r"[\"']([^\"']+\.(?:pdf|mp4|webm|jpe?g|png|gif|webp)(?:\?[^\"']*)?)[\"']",
            value,
            re.IGNORECASE,
        ):
            target = normalize_url(match.group(1), page_url)
            if not target or target in seen:
                continue
            seen.add(target)
            urls.append(target)
            if re.search(r"\.(?:jpe?g|png|gif|webp)$", urlsplit(target).path, re.IGNORECASE):
                images.append(target)
    return urls, images


def extract_page(
    url: str, html: str, content_type: str = "text/html", source_id: str = ""
) -> PageDocument:
    soup = BeautifulSoup(html, "html.parser")
    canonical = _first_meta(soup, "og:url")
    canonical = normalize_url(canonical, url) if canonical else normalize_url(url)
    link = soup.find("link", rel=lambda value: value and "canonical" in value)
    if link and link.get("href"):
        canonical = normalize_url(link["href"], url) or canonical
    detail_heading = soup.select_one(
        ".arti_title, .zkd-title, .articel-show-title, #articel-show-title .n-f-10, "
        ".c-f-30.c-lh-36.n-text-center.n-f-bold, "
        ".News-detail-title, "
        ".article-title, .post-title, .entry-title, .page_title, .detail_title, "
        ".newstitle, .wl-detail-title, .content_title, #Title, "
        ".person-title, .titles, .show01 h5, .center_titlea, .page-header h1, .biaoti_top h1, "
        ".biaoti_top h2, .biaoti_top h3, .bt01"
    )
    title = _text(detail_heading.get_text(" ", strip=True)) if detail_heading else ""
    title_is_heading = bool(title)
    title = title or _first_meta(soup, "og:title", "twitter:title")
    if not title:
        heading = next(
            (
                node
                for node in soup.find_all("h1")
                if _text(node.get_text(" ", strip=True))
            ),
            None,
        )
        title = _text(heading.get_text(" ", strip=True) if heading else "")
        title_is_heading = bool(title)
    if not title and soup.title:
        title = _text(soup.title.get_text(" ", strip=True))
    if not title:
        h2s = [
            node
            for node in soup.find_all("h2")
            if len(_text(node.get_text(" ", strip=True))) >= 4
        ]
        heading = max(h2s, key=lambda node: len(node.get_text(" ", strip=True)), default=None)
        title = _text(heading.get_text(" ", strip=True) if heading else "")
        title_is_heading = bool(title)
    title = _title_from_document(soup, title, url, current_is_heading=title_is_heading)
    metadata = _jsonld_values(soup)
    article_ld = next(
        (
            item
            for item in metadata
            if any(
                t in {"Article", "NewsArticle", "Report"}
                for t in (
                    item.get("@type")
                    if isinstance(item.get("@type"), list)
                    else [item.get("@type")]
                )
            )
        ),
        {},
    )
    date_node = soup.select_one(
        ".media-foucs .date, .detail-content .date, .arti_metas, .ins-res, .detail-info, "
        "#time, .time, .notice_time, .post-meta-side, .post-meta-print, .info-bar, time"
    )
    detail_url = _looks_like_article_url(url)
    detail_date = bool(
        date_node
        and (
            detail_url
            or date_node.get("id") == "time"
            or any(
                token in " ".join(date_node.get("class", []))
                for token in ("detail", "arti_metas", "ins-res", "notice_time", "post-meta")
            )
        )
    )
    path_published = _date_from_url(url)

    def _trust_future_date(value: str, source: str) -> str:
        """Return the date only if it is not an implausible future publication date.

        Event/deadline/effective dates (e.g. ``2026年8月30日试运行`` in a bus
        schedule title) are often placed in the same DOM nodes used for
        publication dates.  Future dates are only trustworthy when they come
        from explicit publication metadata: URL path, JSON-LD, meta tags, or
        a labeled ``发布时间`` string.
        """
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if parsed <= datetime.now().astimezone() + timedelta(days=1):
            return value
        if source in {"path", "jsonld", "meta", "labeled"}:
            return value
        return ""

    explicit_published = path_published
    if not explicit_published:
        value = parse_date(_text(article_ld.get("datePublished")))
        explicit_published = _trust_future_date(value, "jsonld")
    if not explicit_published:
        value = parse_date(
            _first_meta(soup, "article:published_time", "date", "publishdate", "publishTime")
        )
        explicit_published = _trust_future_date(value, "meta")
    if not explicit_published and detail_date:
        date_text_value = date_node.get_text(" ", strip=True)
        labeled = _labeled_date(date_text_value)
        if labeled:
            explicit_published = _trust_future_date(labeled, "labeled")
        else:
            explicit_published = _trust_future_date(parse_date(date_text_value), "unlabeled")
    published = explicit_published
    date_text = date_node.get_text(" ", strip=True) if date_node else ""
    updated = (
        parse_date(_text(article_ld.get("dateModified")))
        or parse_date(_first_meta(soup, "article:modified_time", "lastmod", "modified"))
        or _updated_at_from_time(soup)
        or parse_date(_label_value(date_text, r"更新时间|修改时间"))
    )
    root = _content_root(soup)
    root_is_fallback = root is soup.body or root is soup
    embedded_documents: list[str] = []
    for node in soup.select(
        "[pdfsrc], [swsrc], [vurl], [sudy-wp-src], video[src], audio[src], source[src]"
    ):
        for attribute in ("pdfsrc", "swsrc", "vurl", "sudy-wp-src", "src"):
            target = normalize_url(str(node.get(attribute) or ""), url)
            if target and target not in embedded_documents:
                embedded_documents.append(target)
    player_urls, player_images = _visual_sitebuilder_player_urls(soup, url)
    for target in player_urls:
        if target not in embedded_documents:
            embedded_documents.append(target)
    category = (
        _text(article_ld.get("articleSection"))
        or _first_meta(soup, "article:section", "category")
        or _category_from_breadcrumbs(soup)
        or _first_non_generic_section_heading(soup, title)
    )
    _clean_root(root)
    _remove_repeated_title(root, title)
    if not title:
        # Some legacy USTC templates leave the title element and detail h1 as
        # whitespace while placing the real heading in the first bold lead
        # paragraph.  Recover that short lead without promoting long prose or
        # navigation text to a title.
        for selector in ("h1", "h2", "h3", "p strong", "p b", "p"):
            for node in root.select(selector):
                value = _text(node.get_text(" ", strip=True))
                if 8 <= len(value) <= 160 and not _is_generic_heading(value):
                    title = value
                    break
            if title:
                break
    body_html = str(root)
    body_text = _block_text(root)
    fallback_has_substantive_paragraph = any(
        len(_text(node.get_text(" ", strip=True))) >= 20 for node in root.find_all("p")
    )
    author = (
        _clean_signature_value(_author(article_ld.get("author")))
        or _clean_signature_value(_first_meta(soup, "author", "article:author"))
        or _clean_signature_value(
            _label_value(date_text, r"作者|发布者|文章作者|来源")
        )
        or _trailing_publication_signature(body_text)
    )
    summary = _text(article_ld.get("description")) or _first_meta(
        soup, "description", "og:description"
    )
    images = _image_refs(root, url)
    known_images = {image.url for image in images}
    for target in player_images:
        if target not in known_images:
            known_images.add(target)
            images.append(ImageRef(url=target))
    for image in images:
        image.article_url = canonical or url
    links = []
    link_dates: dict[str, str] = {}
    seen_links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        raw_href = str(anchor["href"])
        # A few legacy USTC templates incorrectly nest a complete <a> element
        # inside the outer href attribute. Recover the inner URL instead of
        # turning the whole markup fragment into a bogus 404 target.
        nested_href = re.search(
            r"<a\b[^>]*\bhref\s*=\s*(['\"])(.*?)\1", raw_href, flags=re.IGNORECASE
        )
        if nested_href:
            raw_href = nested_href.group(2)
        target = normalize_url(raw_href, url)
        if target and target not in seen_links:
            seen_links.add(target)
            links.append(target)
            # Publication dates on listings normally sit beside the anchor.
            # Never infer a hint solely from the anchor text: titles commonly
            # contain event, deadline, or effective dates (for example a bus
            # timetable's trial-operation date), which are not publication
            # dates.
            anchor_text = _text(anchor.get_text(" ", strip=True))
            hint = ""
            contexts = [anchor.parent, anchor.find_parent(["li", "tr"])]
            for context in filter(None, contexts):
                context_text = _text(context.get_text(" ", strip=True))
                surrounding_text = context_text.replace(anchor_text, "", 1).strip()
                hint = _labeled_date(context_text) or parse_date(surrounding_text)
                if hint:
                    break
            if hint:
                link_dates[target] = hint
    for target in embedded_documents:
        if target not in seen_links:
            seen_links.add(target)
            links.append(target)
    attachment_shell = "attachment_id=" in urlsplit(url).query.lower() or "/attachment/" in urlsplit(url).path.lower()
    indico_detail = bool(re.search(r"/event/\d+/(?:page|contributions)/", url, re.I))
    listing_url = bool(
        re.search(r"/(?:list|index)(?:[-_]?\d+)?(?:/|\.[^/?]+)?$", urlsplit(url).path, re.I)
    )
    is_article = (
        bool(article_ld or explicit_published or detail_url or indico_detail)
        and not attachment_shell
        and not listing_url
    )
    if not is_article:
        article_tags = soup.find_all("article")
        is_article = (
            len(article_tags) == 1
            and len(body_text) > 180
            and not attachment_shell
            and not listing_url
        )
    metadata_only = body_text.replace(title, "", 1) if title else body_text
    metadata_only = re.sub(
        r"(?:发布时间|阅读次数|浏览次数|上一篇|下一篇|上一条|下一条|来源|作者)\s*[:：]?",
        "",
        metadata_only,
    )
    metadata_only = re.sub(r"[\s\d|:/：.\-\ue000-\uf8ff]+", "", metadata_only)
    if is_article and not images and not embedded_documents and body_text and not metadata_only:
        is_article = False
    if is_article and root_is_fallback and not fallback_has_substantive_paragraph:
        is_article = False
    if is_article and root_is_fallback and _is_generic_heading(title):
        is_article = False
    if (
        is_article
        and not body_text
        and not embedded_documents
        and title.casefold() in {"banner", "banner3", "banner信息"}
    ):
        is_article = False
    # Some legacy CMS detail URLs return a titled but completely empty HTML
    # shell (the actual page is gone or rendered only by an unavailable
    # client-side request). Keep the raw page and its links, but do not emit a
    # misleading article record with no text or images.
    if is_article and not body_text and not images and not embedded_documents:
        is_article = False
    if is_article and not published:
        # Do not mistake an event/deadline date in a title or the first
        # paragraph for publication time.  Unlabelled body dates are only
        # meaningful as publication dates when a nearby label says so, or
        # when they are the very last content of the article (a common
        # pattern on www.ustc.edu.cn detail pages).
        published = _labeled_date(body_text[:3000])
        if not published:
            published = _labeled_date(body_text[-1500:])
        if not published:
            published = _trailing_body_date(body_text)
    # The ingestion protocol requires a non-empty title.  A few image-only
    # legacy pages expose a publication date and a media element but no title
    # at all; retaining them as articles would create records that cannot be
    # synchronized and are impossible to identify in the public UI.
    if is_article and not title.strip():
        is_article = False
    article = None
    if is_article:
        article = ArticleDocument(
            url=canonical or url,
            source_id=source_id,
            title=title,
            author=author,
            published_at=published,
            updated_at=updated,
            category=category,
            summary=summary,
            body_html=body_html,
            body_text=body_text,
            body_markdown=html_to_markdown(body_html),
            extraction_method="jsonld+meta+heuristic",
            source_page_url=url,
            raw_metadata={"jsonld": metadata},
            images=images,
        )
    adapter = adapter_for(source_id, url)
    if adapter is not None:
        try:
            fields = adapter.extract(url, html)
        except Exception:
            fields = None
        if fields is not None and fields.title.strip():
            body_text = _block_text(BeautifulSoup(fields.body_html, "html.parser"))
            article = ArticleDocument(
                url=canonical or url,
                source_id=source_id,
                title=fields.title,
                author=fields.author,
                published_at=fields.published_at,
                updated_at=updated,
                category=fields.category,
                summary=fields.summary,
                body_html=fields.body_html,
                body_text=body_text,
                body_markdown=html_to_markdown(fields.body_html),
                extraction_method=f"adapter:{adapter.name}",
                source_page_url=url,
                raw_metadata={"jsonld": metadata},
                images=images,
            )
    return PageDocument(
        requested_url=url,
        final_url=url,
        status=200,
        content_type=content_type,
        fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        title=title,
        canonical_url=canonical,
        html=html,
        links=links,
        images=images,
        link_dates=link_dates,
        article=article,
    )
