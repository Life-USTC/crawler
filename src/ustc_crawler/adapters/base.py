"""Per-site extraction adapters.

An adapter recognizes article pages for one site (or one CMS family) and
extracts fields with site-specific selectors.  Sites without an adapter keep
the generic heuristic path unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..markdown import html_to_markdown

__all__ = ["ArticleFields", "SiteAdapter", "html_to_markdown"]


@dataclass(slots=True, frozen=True)
class ArticleFields:
    title: str
    published_at: str
    body_html: str
    author: str = ""
    category: str = ""
    summary: str = ""


class SiteAdapter:
    name: str = ""
    source_ids: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()

    def extract(self, url: str, html: str) -> ArticleFields | None:
        raise NotImplementedError
