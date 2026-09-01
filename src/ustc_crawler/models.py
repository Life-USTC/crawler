from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any


def sanitize_text(value: str) -> str:
    """Remove non-whitespace Unicode control characters from persisted text.

    PostgreSQL rejects U+0000 in text and JSON values.  Preserve ordinary
    layout whitespace (tab, line feed, and carriage return) while removing
    other C0/C1 control characters before a value reaches storage or sync.
    """

    return "".join(
        character
        for character in value
        if character in "\t\n\r" or unicodedata.category(character) != "Cc"
    )


def sanitize_json_value(value: Any) -> Any:
    """Recursively sanitize parser metadata while retaining its JSON shape."""

    if isinstance(value, dict):
        return {
            sanitize_text(str(key)): sanitize_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return sanitize_text(str(value))


@dataclass(slots=True)
class SourceConfig:
    id: str
    name: str
    organization_level: str
    seed_urls: list[str]
    allowed_hosts: list[str]
    blocked_hosts: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    discovery_only: bool = False
    max_images_per_page: int | None = None


@dataclass(slots=True)
class ImageRef:
    url: str
    alt: str = ""
    title: str = ""
    caption: str = ""
    article_url: str = ""


@dataclass(slots=True)
class PageDocument:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    fetched_at: str
    title: str
    canonical_url: str
    html: str
    links: list[str]
    images: list[ImageRef]
    link_dates: dict[str, str] = field(default_factory=dict)
    article: ArticleDocument | None = None
    error: str = ""
    blocked_by_robots: bool = False
    raw_body: bytes | None = None
    page_kind: str = "unknown"
    access_mode: str = "unknown"
    value_score: int = 0
    value_tier: str = "not_indexed"
    score_reasons: list[str] = field(default_factory=list)
    published_at: str = ""
    duplicate_of: str = ""


@dataclass(slots=True)
class ArticleDocument:
    url: str
    source_id: str
    title: str
    author: str
    published_at: str
    updated_at: str
    category: str
    summary: str
    body_html: str
    body_text: str
    body_markdown: str
    extraction_method: str
    source_page_url: str
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    images: list[ImageRef] = field(default_factory=list)
    publication_type: str = ""
    classifier_version: str = ""

    def __post_init__(self) -> None:
        sanitize_article_document(self)


def sanitize_article_document(article: ArticleDocument) -> ArticleDocument:
    """Normalize all article strings at the local archive boundary."""

    for field_name in (
        "url",
        "source_id",
        "title",
        "author",
        "published_at",
        "updated_at",
        "category",
        "summary",
        "body_html",
        "body_text",
        "body_markdown",
        "extraction_method",
        "source_page_url",
        "publication_type",
        "classifier_version",
    ):
        value = getattr(article, field_name)
        setattr(article, field_name, sanitize_text(value) if isinstance(value, str) else "")
    article.raw_metadata = sanitize_json_value(article.raw_metadata)
    for image in article.images:
        image.url = sanitize_text(image.url)
        image.alt = sanitize_text(image.alt)
        image.title = sanitize_text(image.title)
        image.caption = sanitize_text(image.caption)
        image.article_url = sanitize_text(image.article_url)
    return article


@dataclass(slots=True)
class FetchResponse:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    headers: dict[str, str]
    body: bytes
    error: str = ""
    blocked_by_robots: bool = False
