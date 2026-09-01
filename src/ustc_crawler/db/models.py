from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    organization_level: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_hosts: Mapped[str] = mapped_column(Text, nullable=False)
    blocked_hosts: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    seed_urls: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    discovery_only: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_images_per_page: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Frontier(Base):
    __tablename__ = "frontier"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    discovered_from: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index(
            "frontier_priority_idx",
            "status",
            text("priority DESC"),
            "depth",
            "discovered_at",
        ),
        Index(
            "frontier_pending_idx",
            "status",
            text("priority DESC"),
            "depth",
            "discovered_at",
        ),
    )


class Page(Base):
    __tablename__ = "pages"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    discovered_from: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    raw_path: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    blocked_by_robots: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    page_kind: Mapped[str] = mapped_column(Text, nullable=False, default="unknown", server_default="unknown")
    access_mode: Mapped[str] = mapped_column(Text, nullable=False, default="unknown", server_default="unknown")
    value_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    value_tier: Mapped[str] = mapped_column(Text, nullable=False, default="not_indexed", server_default="not_indexed")
    score_reasons: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    published_at: Mapped[str | None] = mapped_column(Text)
    duplicate_of: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("pages_source_idx", "source_id", "fetched_at"),
        Index("pages_value_idx", text("value_score DESC"), "fetched_at"),
    )


class Link(Base):
    __tablename__ = "links"

    source_url: Mapped[str] = mapped_column(Text, primary_key=True)
    target_url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="page")
    discovered_at: Mapped[str] = mapped_column(Text, nullable=False)


class ArticleHint(Base):
    __tablename__ = "article_hints"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    published_at: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Article(Base):
    __tablename__ = "articles"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_markdown: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str | None] = mapped_column(Text)
    source_page_url: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen: Mapped[str] = mapped_column(Text, nullable=False)
    publication_type: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    classifier_version: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    __table_args__ = (Index("articles_date_idx", "published_at"),)


class Media(Base):
    __tablename__ = "media"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    article_url: Mapped[str | None] = mapped_column(ForeignKey("articles.url"))
    source_page_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    alt: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[str | None] = mapped_column(Text)


class ArticleMedia(Base):
    __tablename__ = "article_media"

    article_url: Mapped[str] = mapped_column(ForeignKey("articles.url"), primary_key=True)
    image_url: Mapped[str] = mapped_column(ForeignKey("media.url"), primary_key=True)
    local_path: Mapped[str | None] = mapped_column(Text)
    alt: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Asset(Base):
    __tablename__ = "assets"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    local_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[str | None] = mapped_column(Text)
    page_kind: Mapped[str] = mapped_column(Text, nullable=False, default="document", server_default="document")
    access_mode: Mapped[str] = mapped_column(Text, nullable=False, default="unknown", server_default="unknown")
    value_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    score_reasons: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    articles: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    media: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Failure(Base):
    __tablename__ = "failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    last_seen: Mapped[str] = mapped_column(Text, nullable=False)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="incremental", server_default="incremental")
    source_config_revision: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    digest: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running", server_default="running")
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    articles: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    media: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)


class SyncBatch(Base):
    __tablename__ = "sync_batches"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("sync_runs.id"))
    client_run_id: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    observed_at: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(32), nullable=False)
    producer_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[str | None] = mapped_column(Text)
    response_json: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class SyncOutbox(Base):
    __tablename__ = "sync_outbox"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("sync_batches.id"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("sync_runs.id"))
    source_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_manifest_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[str | None] = mapped_column(Text)
    response_json: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    __table_args__ = (Index("sync_outbox_status_idx", "status", "created_at"),)


class SyncBatchItem(Base):
    __tablename__ = "sync_batch_items"

    batch_id: Mapped[str] = mapped_column(ForeignKey("sync_batches.id"), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    error: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "Article",
    "ArticleHint",
    "ArticleMedia",
    "Asset",
    "Base",
    "Failure",
    "Frontier",
    "Link",
    "Media",
    "Page",
    "Run",
    "Source",
    "SyncBatch",
    "SyncBatchItem",
    "SyncOutbox",
    "SyncRun",
]
