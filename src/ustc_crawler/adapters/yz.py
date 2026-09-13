"""Adapter for yz.ustc.edu.cn (研究生招生在线)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import ArticleFields, SiteAdapter

_ISO_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


class YzAdapter(SiteAdapter):
    name = "yz"
    hosts = ("yz.ustc.edu.cn",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("p.zkd-title")
        body = soup.select_one("div.txt-new")
        if title_node is None or body is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        published = ""
        provenance = soup.select_one("div.provenance")
        if provenance is not None:
            match = _ISO_DATE.search(provenance.get_text(" ", strip=True))
            if match:
                published = match.group(0)
        return ArticleFields(title=title, published_at=published, body_html=str(body))


from . import register  # noqa: E402

register(YzAdapter())
