"""Adapter for the university-wide Vsb CMS (most department sites)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import LABELED_DATE, ArticleFields, SiteAdapter, iso_date_text

_ARTICLE_URL = re.compile(r"/20\d{2}/\d{4}/c\d+a\d+/", re.I)


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
        # Prefer the metadata bar beside the title; related-article lists at
        # the bottom of the page carry the same 发布时间 label and a
        # page-wide search can pick up their date instead of the article's.
        match = None
        metas = soup.select_one(".arti_metas")
        if metas is not None:
            match = LABELED_DATE.search(metas.get_text(" ", strip=True))
        if match is None:
            match = LABELED_DATE.search(soup.get_text(" ", strip=True))
        published = iso_date_text(match) if match else ""
        return ArticleFields(title=title, published_at=published, body_html=str(body))


from . import register  # noqa: E402

register(VsbCmsAdapter())
