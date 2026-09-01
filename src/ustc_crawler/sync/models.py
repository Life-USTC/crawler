from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from ..models import ArticleDocument, SourceConfig
from ..publication import CLASSIFIER_VERSION, PublicationType, classify_publication

INGESTION_PROTOCOL_VERSION = "1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_PUBLICATION_OBJECTS = 100
MAX_PUBLICATION_BATCH_ITEMS = 100
MAX_OBJECT_PLAN_OBJECTS = 100
MAX_PUBLICATION_TITLE_LENGTH = 1_000
MAX_PUBLICATION_AUTHOR_LENGTH = 500
MAX_PUBLICATION_CATEGORY_LENGTH = 500
MAX_PUBLICATION_SUMMARY_LENGTH = 20_000
MAX_PUBLICATION_BODY_TEXT_LENGTH = 5_000_000
MAX_PUBLICATION_EXTRACTION_METHOD_LENGTH = 200

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    ),
]
Url = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Host = Annotated[str, StringConstraints(min_length=1, max_length=253)]
Alias = Annotated[str, StringConstraints(min_length=1, max_length=200)]
ContentType = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=200,
        pattern=r"^[^\s/]+/[A-Za-z0-9!#$&^_.+-]+$",
    ),
]
ObjectKind = Literal["body_html", "body_markdown", "media", "asset", "raw_page"]


class ProtocolModel(BaseModel):
    """A strict immutable model for the public ingestion wire protocol."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


def _validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("must be an absolute HTTP(S) URL")
    return value


def _validate_optional_url(value: str | None) -> str | None:
    if value is not None:
        _validate_url(value)
    return value


def _json_value(value: Any) -> Any:
    """Normalize arbitrary parser metadata into deterministic JSON values."""

    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize_publication_timestamp(value: str | date | datetime) -> str:
    """Normalize USTC date text to an Asia/Shanghai ISO timestamp.

    The crawler database intentionally retains source text.  The server
    accepts date-only and local-naive values, but emitting one explicit
    timezone makes the wire representation unambiguous and stable.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raw = value.strip()
        if not raw:
            return ""
        try:
            if len(raw) == 10:
                parsed = datetime.strptime(raw, "%Y-%m-%d")
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid publication timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    else:
        parsed = parsed.astimezone(SHANGHAI)
    return parsed.isoformat(timespec="seconds")


def _wire_timestamp(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    normalized = normalize_publication_timestamp(value)
    return normalized or None


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value or None


def _bounded_required_text(value: str, max_length: int) -> str:
    return value[:max_length]


def _bounded_optional_text(value: str | None, max_length: int) -> str | None:
    normalized = _optional_text(value)
    return normalized[:max_length] if normalized is not None else None


def _bounded_objects(
    objects: list[ObjectManifest] | tuple[ObjectManifest, ...],
) -> list[ObjectManifest]:
    return list(objects[:MAX_PUBLICATION_OBJECTS])


def _validate_urls(values: list[str]) -> list[str]:
    for value in values:
        _validate_url(value)
    return values


def _validate_optional_urls(values: list[str] | None) -> list[str] | None:
    if values is not None:
        _validate_urls(values)
    return values


def _normalize_required_timestamp(value: str) -> str:
    return normalize_publication_timestamp(value)


def _normalize_optional_timestamp(value: str | None) -> str | None:
    return _wire_timestamp(value)


class ObjectManifest(ProtocolModel):
    """The exact object manifest accepted by the server."""

    kind: ObjectKind
    sha256: Sha256
    size: int = Field(ge=0, le=32 * 1024 * 1024)
    content_type: ContentType = Field(alias="contentType")
    sort_order: int | None = Field(default=None, alias="sortOrder", ge=0, le=10_000)
    alt_text: str | None = Field(default=None, alias="altText", max_length=1_000)


class LocalObjectManifest(ObjectManifest):
    """An object manifest plus the immutable local spool path.

    ``local_path`` exists only in the local outbox manifest.  It is removed
    before serialization of an HTTP payload.
    """

    local_path: str = Field(min_length=1)


class PublicationSourceDescriptor(ProtocolModel):
    """A source snapshot embedded in each immutable ingestion batch."""

    id: SourceId
    name: str = Field(min_length=1, max_length=200)
    organization_level: str | None = Field(
        default=None,
        alias="organizationLevel",
        min_length=1,
        max_length=80,
    )
    allowed_hosts: list[Host] | None = Field(default=None, alias="allowedHosts", max_length=100)
    blocked_hosts: list[Host] | None = Field(default=None, alias="blockedHosts", max_length=100)
    seed_urls: list[Url] | None = Field(default=None, alias="seedUrls", max_length=100)
    aliases: list[Alias] = Field(default_factory=list, max_length=100)
    discovery_only: bool = Field(default=False, alias="discoveryOnly")
    max_images_per_page: int | None = Field(
        default=None,
        alias="maxImagesPerPage",
        gt=0,
        le=1_000,
    )

    _validate_seed_urls = field_validator("seed_urls")(_validate_optional_urls)


class PublicationObjectPlanRequestItem(ProtocolModel):
    """The object identity requested by the server before upload."""

    kind: ObjectKind
    sha256: Sha256


class PublicationObjectPlanRequest(ProtocolModel):
    """Request body for ``/publication-objects/plan``."""

    batch_id: str = Field(alias="batchId", min_length=1, max_length=200)
    objects: list[PublicationObjectPlanRequestItem] = Field(
        min_length=1,
        max_length=MAX_OBJECT_PLAN_OBJECTS,
    )


class PublicationObjectCompleteRequest(ProtocolModel):
    """Request body for ``/publication-objects/complete``."""

    batch_id: str = Field(alias="batchId", min_length=1, max_length=200)
    kind: ObjectKind
    sha256: Sha256


class IngestionItemResult(ProtocolModel):
    """One result returned by the batch ingestion endpoint."""

    source_id: SourceId = Field(alias="sourceId")
    canonical_url: Url = Field(alias="canonicalUrl")
    revision_hash: Sha256 = Field(alias="revisionHash")
    status: Literal["created", "updated", "unchanged", "rejected"]
    publication_id: str | None = Field(alias="publicationId")
    revision_id: str | None = Field(alias="revisionId")
    error: str = Field(default="")

    _validate_canonical_url = field_validator("canonical_url")(_validate_url)


class IngestionBatchResponse(ProtocolModel):
    """Strict response returned by ``/publications/batches``."""

    batch_id: str = Field(alias="batchId", min_length=1)
    client_run_id: str = Field(alias="clientRunId", min_length=1)
    payload_digest: Sha256 = Field(alias="payloadDigest")
    results: list[IngestionItemResult] = Field(
        min_length=1,
        max_length=MAX_PUBLICATION_BATCH_ITEMS,
    )


class RequiredUploadHeaders(ProtocolModel):
    """Headers the server signed into an object upload request."""

    content_type: str = Field(alias="Content-Type", min_length=1)
    metadata_kind: str = Field(alias="x-amz-meta-kind", min_length=1)
    metadata_sha256: str = Field(alias="x-amz-meta-sha256", min_length=1)


class PublicationObjectPlanItem(ProtocolModel):
    """One upload decision returned by the object plan endpoint."""

    kind: ObjectKind
    sha256: Sha256
    r2_key: str = Field(alias="r2Key", min_length=1)
    status: Literal["already_present", "upload_required"]
    upload_url: str | None = Field(alias="uploadUrl")
    expires_at: str | None = Field(alias="expiresAt")
    required_headers: RequiredUploadHeaders = Field(alias="requiredHeaders")

    _validate_upload_url = field_validator("upload_url")(_validate_optional_url)


class PublicationObjectPlanResponse(ProtocolModel):
    """Strict response returned by ``/publication-objects/plan``."""

    batch_id: str = Field(alias="batchId", min_length=1)
    objects: list[PublicationObjectPlanItem] = Field(
        min_length=1,
        max_length=MAX_OBJECT_PLAN_OBJECTS,
    )


class PublicationObjectCompleteResponse(ProtocolModel):
    """Strict response returned by ``/publication-objects/complete``."""

    batch_id: str = Field(alias="batchId", min_length=1)
    kind: ObjectKind
    sha256: Sha256
    status: Literal["verified", "linked"]

class IngestionPublication(ProtocolModel):
    """A non-tombstone item in the server ingestion contract."""

    source_id: SourceId = Field(alias="sourceId")
    canonical_url: Url = Field(alias="canonicalUrl")
    revision_hash: Sha256 = Field(alias="revisionHash")
    observed_at: str = Field(alias="observedAt", min_length=1)
    tombstone: Literal[False] = False
    publication_type: PublicationType = Field(alias="publicationType")
    title: str = Field(min_length=1, max_length=MAX_PUBLICATION_TITLE_LENGTH)
    author: str | None = Field(default=None, max_length=MAX_PUBLICATION_AUTHOR_LENGTH)
    published_at: str | None = Field(default=None, alias="publishedAt")
    updated_at_source: str | None = Field(default=None, alias="updatedAtSource")
    category: str | None = Field(default=None, max_length=MAX_PUBLICATION_CATEGORY_LENGTH)
    summary: str | None = Field(default=None, max_length=MAX_PUBLICATION_SUMMARY_LENGTH)
    body_text: str | None = Field(
        default=None,
        alias="bodyText",
        max_length=MAX_PUBLICATION_BODY_TEXT_LENGTH,
    )
    source_page_url: Url | None = Field(default=None, alias="sourcePageUrl")
    extraction_method: str | None = Field(
        default=None,
        alias="extractionMethod",
        max_length=MAX_PUBLICATION_EXTRACTION_METHOD_LENGTH,
    )
    classifier_version: str | None = Field(default=None, alias="classifierVersion", max_length=200)
    raw_metadata: dict[str, Any] | None = Field(default=None, alias="rawMetadata")
    objects: list[ObjectManifest] = Field(
        default_factory=list,
        max_length=MAX_PUBLICATION_OBJECTS,
    )

    _validate_canonical_url = field_validator("canonical_url")(_validate_url)
    _validate_source_page_url = field_validator("source_page_url")(_validate_optional_url)
    _normalize_observed_at = field_validator("observed_at")(_normalize_required_timestamp)
    _normalize_published_at = field_validator("published_at", "updated_at_source")(
        _normalize_optional_timestamp
    )


class TombstonePublication(ProtocolModel):
    """The strict tombstone branch accepted by the server."""

    source_id: SourceId = Field(alias="sourceId")
    canonical_url: Url = Field(alias="canonicalUrl")
    revision_hash: Sha256 = Field(alias="revisionHash")
    observed_at: str = Field(alias="observedAt", min_length=1)
    tombstone: Literal[True] = True

    _validate_canonical_url = field_validator("canonical_url")(_validate_url)
    _normalize_observed_at = field_validator("observed_at")(_normalize_required_timestamp)


type PublicationItem = IngestionPublication | TombstonePublication


class IngestionBatch(ProtocolModel):
    """The exact immutable JSON body for the publications batch endpoint."""

    protocol_version: Literal["1"] = Field(default=INGESTION_PROTOCOL_VERSION, alias="protocolVersion")
    producer_version: str = Field(alias="producerVersion", min_length=1, max_length=200)
    client_run_id: str = Field(alias="clientRunId", min_length=1, max_length=200)
    batch_id: str = Field(alias="batchId", min_length=1, max_length=200)
    observed_at: str = Field(alias="observedAt", min_length=1)
    sources: list[PublicationSourceDescriptor] = Field(min_length=1, max_length=500)
    items: list[PublicationItem] = Field(min_length=1, max_length=MAX_PUBLICATION_BATCH_ITEMS)

    _normalize_observed_at = field_validator("observed_at")(_normalize_required_timestamp)

    def payload_dict(self) -> dict[str, Any]:
        """Return only server wire keys, with no local paths or credentials."""

        return self.model_dump(by_alias=True, mode="json", exclude_none=True)

    def payload_bytes(self) -> bytes:
        """Return the canonical immutable JSON payload sent to the server."""

        return json.dumps(
            self.payload_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def payload_sha256(self) -> str:
        return hashlib.sha256(self.payload_bytes()).hexdigest()


def _source_descriptor(source: SourceConfig) -> PublicationSourceDescriptor:
    return PublicationSourceDescriptor(
        id=source.id,
        name=source.name,
        organizationLevel=source.organization_level,
        allowedHosts=list(source.allowed_hosts),
        blockedHosts=list(source.blocked_hosts),
        seedUrls=list(source.seed_urls),
        aliases=list(source.aliases),
        discoveryOnly=source.discovery_only,
        maxImagesPerPage=source.max_images_per_page,
    )


def revision_hash_for_article(
    article: ArticleDocument,
    *,
    publication_type: PublicationType | None = None,
    classifier_version: str = CLASSIFIER_VERSION,
    objects: list[ObjectManifest] | tuple[ObjectManifest, ...] = (),
) -> str:
    """Hash every semantic field and content-addressed object reference.

    Observation time is deliberately excluded: seeing unchanged content on a
    later crawl must not create a new server revision.
    """

    kind = publication_type or classify_publication(
        url=article.url,
        source_id=article.source_id,
        title=article.title,
        category=article.category,
        source_page_url=article.source_page_url,
    )
    payload = {
        "sourceId": article.source_id,
        "canonicalUrl": article.url,
        "title": _bounded_required_text(article.title, MAX_PUBLICATION_TITLE_LENGTH),
        "author": _bounded_optional_text(article.author, MAX_PUBLICATION_AUTHOR_LENGTH),
        "publishedAt": _wire_timestamp(article.published_at),
        "updatedAtSource": _wire_timestamp(article.updated_at),
        "category": _bounded_optional_text(article.category, MAX_PUBLICATION_CATEGORY_LENGTH),
        "summary": _bounded_optional_text(article.summary, MAX_PUBLICATION_SUMMARY_LENGTH),
        "bodyText": _bounded_optional_text(article.body_text, MAX_PUBLICATION_BODY_TEXT_LENGTH),
        "sourcePageUrl": article.source_page_url or article.url,
        "extractionMethod": _bounded_optional_text(
            article.extraction_method,
            MAX_PUBLICATION_EXTRACTION_METHOD_LENGTH,
        ),
        "classifierVersion": classifier_version,
        "publicationType": kind,
        "rawMetadata": _json_value(article.raw_metadata),
        "objects": [
            manifest.model_dump(by_alias=True, mode="json", exclude_none=True)
            for manifest in sorted(
                _bounded_objects(objects),
                key=lambda item: (
                    item.kind,
                    item.sort_order if item.sort_order is not None else -1,
                    item.sha256,
                ),
            )
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_publication(
    article: ArticleDocument,
    *,
    objects: list[ObjectManifest] | tuple[ObjectManifest, ...] = (),
    publication_type: PublicationType | None = None,
    classifier_version: str = CLASSIFIER_VERSION,
    observed_at: str | date | datetime | None = None,
) -> IngestionPublication:
    kind = publication_type or classify_publication(
        url=article.url,
        source_id=article.source_id,
        title=article.title,
        category=article.category,
        source_page_url=article.source_page_url,
    )
    revision_hash = revision_hash_for_article(
        article,
        publication_type=kind,
        classifier_version=classifier_version,
        objects=list(objects),
    )
    observation = observed_at or datetime.now(SHANGHAI)
    return IngestionPublication(
        sourceId=article.source_id,
        canonicalUrl=article.url,
        revisionHash=revision_hash,
        observedAt=normalize_publication_timestamp(observation),
        publicationType=kind,
        title=_bounded_required_text(article.title, MAX_PUBLICATION_TITLE_LENGTH),
        author=_bounded_optional_text(article.author, MAX_PUBLICATION_AUTHOR_LENGTH),
        publishedAt=_wire_timestamp(article.published_at),
        updatedAtSource=_wire_timestamp(article.updated_at),
        category=_bounded_optional_text(article.category, MAX_PUBLICATION_CATEGORY_LENGTH),
        summary=_bounded_optional_text(article.summary, MAX_PUBLICATION_SUMMARY_LENGTH),
        bodyText=_bounded_optional_text(article.body_text, MAX_PUBLICATION_BODY_TEXT_LENGTH),
        sourcePageUrl=article.source_page_url or article.url,
        extractionMethod=_bounded_optional_text(
            article.extraction_method,
            MAX_PUBLICATION_EXTRACTION_METHOD_LENGTH,
        ),
        classifierVersion=classifier_version,
        rawMetadata=_json_value(article.raw_metadata) or None,
        objects=_bounded_objects(objects),
    )


def build_ingestion_batch(
    items: Iterable[PublicationItem],
    *,
    sources: Iterable[PublicationSourceDescriptor],
    client_run_id: str,
    batch_id: str,
    observed_at: str | date | datetime,
    producer_version: str,
) -> IngestionBatch:
    return IngestionBatch(
        producerVersion=producer_version,
        clientRunId=client_run_id,
        batchId=batch_id,
        observedAt=normalize_publication_timestamp(observed_at),
        sources=list(sources),
        items=list(items),
    )


__all__ = [
    "INGESTION_PROTOCOL_VERSION",
    "MAX_PUBLICATION_BATCH_ITEMS",
    "MAX_OBJECT_PLAN_OBJECTS",
    "IngestionBatch",
    "IngestionBatchResponse",
    "IngestionItemResult",
    "IngestionPublication",
    "LocalObjectManifest",
    "ObjectKind",
    "ObjectManifest",
    "PublicationObjectCompleteRequest",
    "PublicationObjectCompleteResponse",
    "PublicationObjectPlanItem",
    "PublicationObjectPlanRequestItem",
    "PublicationObjectPlanRequest",
    "PublicationObjectPlanResponse",
    "PublicationItem",
    "PublicationSourceDescriptor",
    "RequiredUploadHeaders",
    "TombstonePublication",
    "build_ingestion_batch",
    "build_publication",
    "normalize_publication_timestamp",
    "revision_hash_for_article",
    "_source_descriptor",
]
