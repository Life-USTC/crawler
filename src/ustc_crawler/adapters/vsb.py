"""Adapter for the university-wide Vsb CMS (most department sites)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import ArticleFields, SiteAdapter

_ARTICLE_URL = re.compile(r"/20\d{2}/\d{4}/c\d+a\d+/", re.I)
_DATE_LABEL = re.compile(r"发布时间[：:]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})")


class VsbCmsAdapter(SiteAdapter):
    name = "vsb"
    hosts = ("mech.ustc.edu.cn", "iid.ustc.edu.cn")

    def extract(self, url: str, html: str) -> ArticleFields | None:
        if not _ARTICLE_URL.search(url):
            return None
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one(".arti_title")
        body = soup.select_one(".wp_articlecontent")
        if title_node is None or body is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        match = _DATE_LABEL.search(soup.get_text(" ", strip=True))
        published = (
            f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
            if match
            else ""
        )
        return ArticleFields(title=title, published_at=published, body_html=str(body))


from . import register  # noqa: E402

register(VsbCmsAdapter())
