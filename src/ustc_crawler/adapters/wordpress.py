"""Adapter for WordPress-based sites (library and friends)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from .base import ISO_DATE, ArticleFields, SiteAdapter, iso_date_text


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
        # The library template renders exactly one h1 in the detail
        # container (the post title); the first h1 is assumed to be it.
        title_node = container.find("h1")
        # Match on the h2's full text so a date wrapped in a nested inline
        # element (e.g. ``<h2><span>2026-01-05</span></h2>``) is still found.
        date_node = next(
            (
                h2
                for h2 in container.find_all("h2")
                if ISO_DATE.search(h2.get_text(" ", strip=True))
            ),
            None,
        )
        if title_node is None or date_node is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        match = ISO_DATE.search(date_node.get_text(" ", strip=True))
        assert match is not None
        published = iso_date_text(match)
        for node in (title_node, date_node):
            node.extract()
        body = container.get_text(" ", strip=True)
        if len(body) < 10:
            return None
        return ArticleFields(title=title, published_at=published, body_html=str(container))


from . import register  # noqa: E402

register(WordPressAdapter())
