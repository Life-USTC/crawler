"""Adapter for the .jhtml CMS used by iat.ustc.edu.cn and friends."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import ArticleFields, SiteAdapter

_DATE_LABEL = re.compile(r"发布时间[：:]\s*(20\d{2})-(\d{2})-(\d{2})")


class JhtmlAdapter(SiteAdapter):
    name = "jhtml"
    hosts = ("iat.ustc.edu.cn",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("p.News-detail-title")
        body = soup.select_one("div.news-detail-news-con")
        if title_node is None or body is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        notes = soup.select_one("div.news-detail-notes")
        published = ""
        if notes is not None:
            match = _DATE_LABEL.search(notes.get_text(" ", strip=True))
            if match:
                published = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        return ArticleFields(title=title, published_at=published, body_html=str(body))


from . import register  # noqa: E402

register(JhtmlAdapter())
