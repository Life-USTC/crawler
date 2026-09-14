"""Convert extracted article body HTML to clean Markdown."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from .canonicalize import normalize_url


class _ArticleConverter(MarkdownConverter):
    def convert_img(self, el, text, parent_tags):
        # Base64-inlined images are unusable in Markdown and can be megabytes.
        src = el.get("src") or ""
        if src.startswith("data:"):
            return ""
        return super().convert_img(el, text, parent_tags)


def _absolutize_images(soup: BeautifulSoup, base_url: str) -> None:
    """Rewrite relative/lazy image URLs against the page URL in place.

    Site-relative ``src`` values (e.g. ``/__local/...``) break as soon as the
    Markdown leaves the dashboard, and lazy-loaded images only carry
    ``data-src``.  Normalize both, plus ``srcset`` candidates, when the page
    URL is known.
    """
    for image in soup.find_all("img"):
        raw = (
            image.get("src")
            or image.get("data-src")
            or image.get("data-original")
            or image.get("data-lazy-src")
        )
        if raw:
            absolute = normalize_url(str(raw), base_url)
            if absolute:
                image["src"] = absolute
        srcset = str(image.get("srcset") or "")
        if srcset:
            candidates = []
            for part in srcset.split(","):
                bits = part.split()
                if not bits:
                    continue
                absolute = normalize_url(bits[0], base_url)
                candidates.append(" ".join([absolute or bits[0], *bits[1:]]))
            if candidates:
                image["srcset"] = ", ".join(candidates)


def html_to_markdown(
    html: str, *, strip_selectors: tuple[str, ...] = (), base_url: str = ""
) -> str:
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(["script", "style", "form", "noscript"]):
        node.decompose()
    for selector in strip_selectors:
        for node in soup.select(selector):
            node.decompose()
    if base_url:
        _absolutize_images(soup, base_url)
    md = _ArticleConverter(heading_style="ATX", bullets="-").convert_soup(soup)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()
