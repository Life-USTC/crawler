"""Convert extracted article body HTML to clean Markdown."""

from __future__ import annotations

import hashlib
import html as html_module
import re
from collections.abc import Mapping

from bs4 import BeautifulSoup, NavigableString, Tag
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

IMAGE_PROXY_PREFIX = "/api/publications/images/"


def image_source_hash(url: str) -> str:
    """Return the stable local-image identity for an absolute source URL."""

    return hashlib.sha256(url.encode("utf-8")).hexdigest().lower()


def local_image_url(url: str) -> str:
    """Return the root-relative public proxy path for one source URL."""

    return f"{IMAGE_PROXY_PREFIX}{image_source_hash(url)}"


class _ArticleConverter(MarkdownConverter):
    def escape(self, text, parent_tags):
        """Escape literal punctuation that can join generated Markdown syntax.

        ``markdownify`` escapes most Markdown punctuation, but leaves ``!``
        untouched.  A source text node ending in ``!`` immediately before an
        HTML link therefore joins the link's generated ``[...](...)`` syntax
        and becomes an image.  Escape the source character while preserving
        the converter's normal handling of preformatted/code content.
        """
        text = super().escape(text, parent_tags)
        if "_noformat" not in parent_tags:
            text = text.replace("!", r"\!")
        return text

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
            # VSB puts ``vurl`` on ordinary images as well as on its video
            # player containers.  An image's attribute is metadata for the
            # image and must not turn the image into a video link.
            if node.name == "img":
                continue
            raw = str(node.get(attribute) or "").strip()
            if not raw:
                continue
            target = normalize_url(raw, base_url) if base_url else ""
            target = target or raw
            anchor = soup.new_tag("a", href=target)
            anchor.string = _media_label(node, label)
            node.replace_with(anchor)


def _replace_videos(soup: BeautifulSoup, base_url: str) -> None:
    """Render native videos as links without leaking poster images.

    ``markdownify`` treats a ``video[poster]`` as an image wrapped in a link.
    Poster URLs are not article image sources, so retaining that output would
    produce a Markdown image with no registered local proxy.  Keep the video
    itself useful by linking to its first HTTP(S) source and discard the
    poster/fallback markup.
    """
    for node in soup.find_all("video"):
        raw = str(node.get("src") or "").strip()
        if not raw:
            source = node.find("source", src=True)
            raw = str(source.get("src") or "").strip() if source else ""
        if not raw:
            node.decompose()
            continue
        target = normalize_url(raw, base_url) if base_url else ""
        target = target or raw
        anchor = soup.new_tag("a", href=target)
        anchor.string = _media_label(node, "视频")
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


def _absolutize_images(
    soup: BeautifulSoup,
    base_url: str,
    image_sources: Mapping[str, str] | None = None,
    *,
    strict_image_sources: bool = False,
) -> None:
    """Rewrite relative/lazy image URLs against the page URL in place.

    Site-relative ``src`` values (e.g. ``/__local/...``) break as soon as the
    Markdown leaves the dashboard, and lazy-loaded images only carry
    ``data-src``.  Normalize both, plus ``srcset`` candidates, when the page
    URL is known.
    """
    for image in soup.find_all("img"):
        raw = (
            image.get("data-src")
            or image.get("data-original")
            or image.get("data-lazy-src")
            or image.get("src")
        )
        srcset = str(image.get("srcset") or "")
        srcset_candidates: list[str] = []
        if srcset:
            for part in srcset.split(","):
                bits = part.split()
                if not bits:
                    continue
                absolute = normalize_url(bits[0], base_url)
                candidate = absolute or bits[0]
                if image_sources is not None and absolute:
                    registered = image_sources.get(image_source_hash(absolute))
                    if registered == absolute:
                        candidate = local_image_url(absolute)
                srcset_candidates.append(" ".join([candidate, *bits[1:]]))
            if srcset_candidates:
                image["srcset"] = ", ".join(srcset_candidates)
                if not raw:
                    raw = srcset.split(",", 1)[0].strip().split(" ", 1)[0]
        if raw:
            absolute = normalize_url(str(raw), base_url)
            if absolute:
                destination = absolute
                if image_sources is not None:
                    registered = image_sources.get(image_source_hash(absolute))
                    if registered != absolute:
                        if strict_image_sources:
                            image.decompose()
                            continue
                    else:
                        destination = local_image_url(absolute)
                image["src"] = destination
            elif strict_image_sources and image_sources is not None:
                image.decompose()
        elif strict_image_sources and image_sources is not None:
            image.decompose()


def _strip_paragraph_layout_whitespace(soup: BeautifulSoup) -> None:
    """Remove source indentation from paragraphs before Markdown conversion.

    A number of Chinese CMS templates put a full-width or non-breaking space
    at the start of every paragraph.  The public article renderer supplies
    paragraph indentation itself, so retaining those characters creates a
    visibly doubled indent.  Restrict the cleanup to the first text run in a
    ``p`` element; list and code indentation remains semantic.
    """

    layout_chars = " \t\r\n\xa0\u3000"
    layout = re.compile(r"^[ \t\r\n\xa0\u3000]+")
    for paragraph in soup.find_all("p"):
        if paragraph.find_parent(["pre", "code", "li"]):
            continue
        for node in paragraph.descendants:
            if not isinstance(node, NavigableString):
                continue
            if node.find_parent(["pre", "code"]):
                break
            # Once an image or another block/line-break element has occurred,
            # following whitespace separates content from a caption/text run;
            # it is no longer paragraph-leading layout whitespace.
            has_rendered_content = False
            for previous in node.previous_elements:
                if previous is paragraph:
                    break
                if isinstance(previous, Tag) and previous.name in {
                    "img",
                    "br",
                    "hr",
                    "table",
                    "ul",
                    "ol",
                    "pre",
                    "code",
                }:
                    has_rendered_content = True
                    break
            if has_rendered_content:
                break
            value = str(node)
            normalized = layout.sub("", value)
            if normalized != value:
                node.replace_with(normalized)
            if value.strip(layout_chars):
                break


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


def _prepare_soup(
    html: str,
    *,
    base_url: str,
    strip_selectors: tuple[str, ...],
) -> BeautifulSoup:
    soup = BeautifulSoup(_restore_escaped_tags(html), "html.parser")
    _inline_pdf_viewer_images(soup, base_url)
    for node in soup.find_all(["script", "style", "form", "noscript"]):
        node.decompose()
    _replace_players(soup, base_url)
    _replace_videos(soup, base_url)
    _drop_empty_headings(soup)
    _strip_noise(soup, strip_selectors)
    _strip_paragraph_layout_whitespace(soup)
    return soup


def image_source_urls(
    html: str,
    *,
    strip_selectors: tuple[str, ...] = (),
    base_url: str = "",
) -> tuple[str, ...]:
    """Return image sources from the same normalized DOM used by conversion."""

    if not html or not html.strip():
        return ()
    soup = _prepare_soup(
        html,
        base_url=base_url,
        strip_selectors=strip_selectors,
    )
    result: list[str] = []
    seen: set[str] = set()
    for image in soup.find_all("img"):
        raw = (
            image.get("data-src")
            or image.get("data-original")
            or image.get("data-lazy-src")
            or image.get("src")
        )
        if not raw and image.get("srcset"):
            raw = str(image["srcset"]).split(",", 1)[0].strip().split(" ", 1)[0]
        source_url = normalize_url(str(raw or ""), base_url)
        if source_url and source_url not in seen:
            seen.add(source_url)
            result.append(source_url)
    return tuple(result)


def html_to_markdown(
    html: str,
    *,
    strip_selectors: tuple[str, ...] = (),
    base_url: str = "",
    image_sources: Mapping[str, str] | None = None,
    strict_image_sources: bool = False,
) -> str:
    if not html or not html.strip():
        return ""
    soup = _prepare_soup(
        html,
        base_url=base_url,
        strip_selectors=strip_selectors,
    )
    if base_url:
        _absolutize_images(
            soup,
            base_url,
            image_sources,
            strict_image_sources=strict_image_sources,
        )
        _absolutize_links(soup, base_url)
    md = _ArticleConverter(heading_style="ATX", bullets="-").convert_soup(soup)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()
