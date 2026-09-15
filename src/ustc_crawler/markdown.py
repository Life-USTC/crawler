"""Convert extracted article body HTML to clean Markdown."""

from __future__ import annotations

import html as html_module
import re

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from .canonicalize import normalize_url

# Site-chrome blocks that leak into extracted body HTML across the VSB/Joomla
# CMS family: share buttons, visit counters, metadata bars, pagers, and
# footer friend-link modules.
_NOISE_SELECTORS = (
    ".social-share",
    ".share_tt",
    ".WP_VisitCount",
    ".wl-post",
    ".weix_time",
    "ul.pagenav",
    ".pagination",
    ".linkstitle",
    ".bottomlinks",
)

# FCKeditor-era articles store markup as escaped text (``&lt;IMG ...&gt;``),
# often without the closing ``&gt;`` — the text runs into the next real tag.
# Restore it only when the page carries several such tags so prose that
# legitimately shows an escaped tag example stays untouched.
_ESCAPED_TAG = re.compile(
    r"&lt;(/?(?:img|table|tr|td|th|p|br)\b[^<>]*?)(?:&gt;|(?=<)|$)",
    re.IGNORECASE | re.DOTALL,
)
_ESCAPED_TAG_TRIGGER = re.compile(r"&lt;(?:img|table|p|br)\b", re.IGNORECASE)
_ESCAPED_TAG_THRESHOLD = 3

# ``sudyfile-attr="{'title':'...'}"`` carries the media's display name.
_SUDYFILE_TITLE = re.compile(r"'title'\s*:\s*'([^']*)'")

# VisualSiteBuilder embeds the real media URL in element attributes while the
# player itself renders via JavaScript, so the container converts to nothing.
_PLAYER_URL_ATTRS = (
    ("sudy-wp-src", "视频"),
    ("pdfsrc", "附件"),
    ("swsrc", "附件"),
    ("vurl", "视频"),
)

_HEADING_TAGS = re.compile(r"^h[1-6]$")

# Residue line left after a visit counter/share widget is stripped, e.g.
# ``2026-07-06 | 查看:`` — only dates, separators, and counter vocabulary.
_COUNTER_LINE = re.compile(r"^[\s\d|｜:：/.\-]*(查看|访问次数|分享至)?[\s\d|｜:：/.\-次]*$")

_VSB_PDF_IMAGE = re.compile(
    r"[\"']([^\"']+\.(?:jpe?g|png|gif|webp)(?:\?[^\"']*)?)[\"']", re.IGNORECASE
)


class _ArticleConverter(MarkdownConverter):
    def convert_img(self, el, text, parent_tags):
        # Base64-inlined images are unusable in Markdown and can be megabytes.
        src = el.get("src") or ""
        if src.startswith("data:"):
            return ""
        return super().convert_img(el, text, parent_tags)


def _restore_escaped_tags(html: str) -> str:
    """Unescape FCKeditor-style escaped tags when they dominate the markup."""
    if len(_ESCAPED_TAG_TRIGGER.findall(html)) < _ESCAPED_TAG_THRESHOLD:
        return html
    return _ESCAPED_TAG.sub(
        lambda match: html_module.unescape(f"&lt;{match.group(1)}&gt;"), html
    )


def _media_label(node, fallback: str) -> str:
    match = _SUDYFILE_TITLE.search(str(node.get("sudyfile-attr") or ""))
    title = match.group(1).strip() if match else ""
    return f"{fallback}: {title}" if title else fallback


def _replace_players(soup: BeautifulSoup, base_url: str) -> None:
    """Swap JS-only player containers for a link to the underlying media file."""
    for attribute, label in _PLAYER_URL_ATTRS:
        for node in soup.select(f"[{attribute}]"):
            raw = str(node.get(attribute) or "").strip()
            if not raw:
                continue
            target = normalize_url(raw, base_url) if base_url else ""
            target = target or raw
            anchor = soup.new_tag("a", href=target)
            anchor.string = _media_label(node, label)
            node.replace_with(anchor)


def _inline_pdf_viewer_images(soup: BeautifulSoup, base_url: str) -> None:
    """Expand ``vsb_pdf_image_data`` viewer scripts into plain <img> tags.

    VSB renders PDF scans through JavaScript; without this the article body
    converts to nothing at all.
    """
    for script in soup.find_all("script"):
        value = script.string or script.get_text()
        if "vsb_pdf_image_data" not in value:
            continue
        urls = _VSB_PDF_IMAGE.findall(value)
        if not urls:
            continue
        container = soup.new_tag("p")
        for raw in urls:
            src = normalize_url(raw, base_url) if base_url else ""
            image = soup.new_tag("img", src=src or raw)
            container.append(image)
            container.append("\n")
        script.replace_with(container)


def _drop_empty_headings(soup: BeautifulSoup) -> None:
    """Remove empty headings; unwrap image-only ones so the image survives."""
    for node in soup.find_all(_HEADING_TAGS):
        if node.get_text(strip=True):
            continue
        images = node.find_all("img")
        if images:
            node.replace_with(*list(node.children))
        else:
            node.decompose()


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


def _absolutize_links(soup: BeautifulSoup, base_url: str) -> None:
    """Rewrite relative link hrefs (attachments, site pages) against base_url.

    ``normalize_url`` rejects non-http schemes, so ``mailto:``,
    ``javascript:``, and ``data:`` values pass through unchanged; in-page
    ``#fragment`` anchors are explicitly kept as-is.
    """
    for anchor in soup.find_all("a", href=True):
        raw = str(anchor["href"]).strip()
        if not raw or raw.startswith("#"):
            continue
        absolute = normalize_url(raw, base_url)
        if absolute:
            anchor["href"] = absolute


def _strip_noise(soup: BeautifulSoup, strip_selectors: tuple[str, ...]) -> None:
    for selector in [*_NOISE_SELECTORS, *strip_selectors]:
        for node in soup.select(selector):
            parent = node.parent
            node.decompose()
            # Dropping a counter widget leaves a husk line like
            # ``2026-07-06 | 查看:``; remove the wrapping element when nothing
            # but counter/date residue remains.
            if (
                parent is not None
                and parent.name in {"p", "div", "span"}
                and parent.find(["a", "img"]) is None
                and len(parent.get_text(strip=True)) <= 30
                and _COUNTER_LINE.match(parent.get_text(strip=True))
            ):
                parent.decompose()


def html_to_markdown(
    html: str, *, strip_selectors: tuple[str, ...] = (), base_url: str = ""
) -> str:
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(_restore_escaped_tags(html), "html.parser")
    _inline_pdf_viewer_images(soup, base_url)
    for node in soup.find_all(["script", "style", "form", "noscript"]):
        node.decompose()
    _replace_players(soup, base_url)
    _drop_empty_headings(soup)
    _strip_noise(soup, strip_selectors)
    if base_url:
        _absolutize_images(soup, base_url)
        _absolutize_links(soup, base_url)
    md = _ArticleConverter(heading_style="ATX", bullets="-").convert_soup(soup)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()
