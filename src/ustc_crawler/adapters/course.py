"""Adapter for course.ustc.edu.cn (瀚海教学网 portal news)."""

from __future__ import annotations

from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import ISO_DATE, LABELED_DATE, ArticleFields, SiteAdapter, iso_date_text


class CourseAdapter(SiteAdapter):
    name = "course"
    hosts = ("course.ustc.edu.cn",)

    def extract(self, url: str, html: str) -> ArticleFields | None:
        if urlsplit(url).path.rstrip("/") != "/portal/news/info":
            return None
        soup = BeautifulSoup(html, "html.parser")
        container = soup.select_one("div.infion-con")
        body = soup.select_one("div.Content")
        if container is None or body is None:
            return None
        title_node = container.find("span")
        if title_node is None:
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        # The info header sometimes carries a publication date next to the
        # title; leave it empty otherwise so generic extraction can fill in.
        container_text = container.get_text(" ", strip=True)
        match = LABELED_DATE.search(container_text) or ISO_DATE.search(container_text)
        published = iso_date_text(match) if match else ""
        return ArticleFields(title=title, published_at=published, body_html=str(body))


from . import register  # noqa: E402

register(CourseAdapter())
