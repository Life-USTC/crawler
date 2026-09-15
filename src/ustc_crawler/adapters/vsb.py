"""Adapter for the university-wide Vsb CMS (most department sites)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import LABELED_DATE, ArticleFields, SiteAdapter, iso_date_text

_ARTICLE_URL = re.compile(r"/20\d{2}/\d{4}/c\d+a\d+/", re.I)

# physics.ustc.edu.cn stamps a bracketed publication date into the heading
# itself (``<font size="-1">[2025-09-18]</font>``); it is metadata, not title.
_BRACKETED_DATE_SUFFIX = re.compile(r"\s*\[\d{4}-\d{2}-\d{2}\]\s*$")


class VsbCmsAdapter(SiteAdapter):
    name = "vsb"
    hosts = (
        "mech.ustc.edu.cn",
        "iid.ustc.edu.cn",
        "www.nsrl.ustc.edu.cn",
        "physics.ustc.edu.cn",
    )

    def extract(self, url: str, html: str) -> ArticleFields | None:
        if not _ARTICLE_URL.search(url):
            return None
        soup = BeautifulSoup(html, "html.parser")
        title_node = next(
            (
                node
                for node in soup.select(".arti_title, .article-tit h1")
                if node.get_text(" ", strip=True)
            ),
            None,
        )
        body = soup.select_one(".wp_articlecontent")
        if title_node is None or body is None:
            return None
        title = _BRACKETED_DATE_SUFFIX.sub(
            "", title_node.get_text(" ", strip=True)
        ).strip()
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
