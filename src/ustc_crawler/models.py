from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
