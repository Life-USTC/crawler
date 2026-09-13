"""Adapter for WordPress-based sites (library and friends)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import ArticleFields, SiteAdapter

_ISO_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


class WordPressAdapter(SiteAdapter):
    name = "wordpress"
    hosts = ("lib.ustc.edu.cn",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        soup = BeautifulSoup(html, "html.parser")
        container = soup.select_one("div.detail-text") or soup.select_one(
            "div.detail-content"
        )
        if container is None:
            return None
        title_node = container.find("h1")
        date_node = container.find("h2", string=_ISO_DATE)
        if title_node is None or date_node is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        published = _ISO_DATE.search(date_node.get_text()).group(0)
        for node in (title_node, date_node):
            node.extract()
        body = container.get_text(" ", strip=True)
        if len(body) < 10:
            return None
        return ArticleFields(title=title, published_at=published, body_html=str(container))


from . import register  # noqa: E402

register(WordPressAdapter())
