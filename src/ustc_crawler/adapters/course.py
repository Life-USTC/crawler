"""Adapter for course.ustc.edu.cn (瀚海教学网 portal news)."""

from __future__ import annotations

from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import ArticleFields, SiteAdapter


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
        return ArticleFields(title=title, published_at="", body_html=str(body))


from . import register  # noqa: E402

register(CourseAdapter())
