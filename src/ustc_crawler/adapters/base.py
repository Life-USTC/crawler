"""Per-site extraction adapters.

An adapter recognizes article pages for one site (or one CMS family) and
extracts fields with site-specific selectors.  Sites without an adapter keep
the generic heuristic path unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ArticleFields", "ISO_DATE", "LABELED_DATE", "SiteAdapter", "iso_date_text"]

# Shared date patterns.  Both accept single-digit month/day; ``iso_date_text``
# zero-pads the captured groups so every adapter emits the same ISO shape.
ISO_DATE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
LABELED_DATE = re.compile(r"发布时间[：:]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})")


def iso_date_text(match: re.Match[str]) -> str:
    """Format a match of ``ISO_DATE``/``LABELED_DATE`` as zero-padded ISO."""
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


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
