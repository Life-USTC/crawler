"""Convert extracted article body HTML to clean Markdown."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


class _ArticleConverter(MarkdownConverter):
    def convert_img(self, el, text, parent_tags):
        # Base64-inlined images are unusable in Markdown and can be megabytes.
        src = el.get("src") or ""
        if src.startswith("data:"):
            return ""
        return super().convert_img(el, text, parent_tags)


def html_to_markdown(html: str, *, strip_selectors: tuple[str, ...] = ()) -> str:
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(["script", "style", "form", "noscript"]):
        node.decompose()
    for selector in strip_selectors:
        for node in soup.select(selector):
            node.decompose()
    md = _ArticleConverter(heading_style="ATX", bullets="-").convert_soup(soup)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()
