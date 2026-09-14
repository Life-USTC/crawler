from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from ..models import ArticleDocument, SourceConfig, sanitize_json_value, sanitize_text
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


def _strip_text(value: Any) -> Any:
    """Mirror the ingestion server's Zod ``string().trim()`` transform."""

    return sanitize_text(value).strip() if isinstance(value, str) else value


def _sanitize_optional_text(value: Any) -> Any:
    """Sanitize optional wire text without coercing or iterating over ``None``."""

    return sanitize_text(value) if isinstance(value, str) else value


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


SourceId = Annotated[
    str,
    BeforeValidator(_strip_text),
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    ),
]
Url = Annotated[
    str,
    BeforeValidator(_strip_text),
    StringConstraints(min_length=1, max_length=2048),
]
Host = Annotated[
    str,
    BeforeValidator(_strip_text),
    StringConstraints(min_length=1, max_length=253),
]
Alias = Annotated[
    str,
    BeforeValidator(_strip_text),
    StringConstraints(min_length=1, max_length=200),
]
ContentType = Annotated[
    str,
    BeforeValidator(_strip_text),
    StringConstraints(
        min_length=1,
        max_length=200,
        pattern=r"^[^\s/]+/[A-Za-z0-9!#$&^_.+-]+$",
    ),
]
TrimmedText = Annotated[str, BeforeValidator(_strip_text)]
TrimmedOptionalText = Annotated[str | None, BeforeValidator(_strip_text)]
SanitizedText = Annotated[str, BeforeValidator(sanitize_text)]
SanitizedOptionalText = Annotated[str | None, BeforeValidator(_sanitize_optional_text)]
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

    return sanitize_json_value(value)


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
        raw = sanitize_text(value).strip()
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
    # The server trims before validating its bounds.  Trim again after the
    # bound so a truncation boundary cannot leave whitespace for the server to
    # transform a second time (and therefore change the batch digest).
    normalized = sanitize_text(value).strip()[:max_length].strip()
    if not normalized:
        raise ValueError("required publication text must not be blank")
    return normalized


def _bounded_optional_text(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = sanitize_text(value).strip()[:max_length].strip()
    return normalized or None


def _bounded_body_text(value: str | None, max_length: int) -> str | None:
    """Bound body text without trimming; the server deliberately preserves it."""

    normalized = sanitize_text(value) if value else _optional_text(value)
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
    alt_text: TrimmedOptionalText = Field(default=None, alias="altText", max_length=1_000)


class LocalObjectManifest(ObjectManifest):
    """An object manifest plus the immutable local spool path.

    ``local_path`` exists only in the local outbox manifest.  It is removed
    before serialization of an HTTP payload.
    """

    local_path: str = Field(min_length=1)


class PublicationSourceDescriptor(ProtocolModel):
    """A source snapshot embedded in each immutable ingestion batch."""

    id: SourceId
    name: TrimmedText = Field(min_length=1, max_length=200)
    organization_level: TrimmedOptionalText = Field(
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

    batch_id: TrimmedText = Field(alias="batchId", min_length=1, max_length=200)
    objects: list[PublicationObjectPlanRequestItem] = Field(
        min_length=1,
        max_length=MAX_OBJECT_PLAN_OBJECTS,
    )


class ObjectNeedingUpload(ProtocolModel):
    """An object whose bytes the server is missing for an unchanged item."""

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
    objects_needing_upload: list[ObjectNeedingUpload] | None = Field(
        default=None,
        alias="objectsNeedingUpload",
    )

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
    """Headers required by the authenticated Worker upload request."""

    content_type: str = Field(alias="Content-Type", min_length=1)


class PublicationObjectPlanItem(ProtocolModel):
    """One upload decision returned by the object plan endpoint."""

    kind: ObjectKind
    sha256: Sha256
    r2_key: str = Field(alias="r2Key", min_length=1)
    status: Literal["already_present", "upload_required"]
    upload_url: str | None = Field(alias="uploadUrl")
    required_headers: RequiredUploadHeaders = Field(alias="requiredHeaders")

    _validate_upload_url = field_validator("upload_url")(_validate_optional_url)


class PublicationObjectPlanResponse(ProtocolModel):
    """Strict response returned by ``/publication-objects/plan``."""

    batch_id: str = Field(alias="batchId", min_length=1)
    objects: list[PublicationObjectPlanItem] = Field(
        min_length=1,
        max_length=MAX_OBJECT_PLAN_OBJECTS,
    )


class PublicationObjectUploadResponse(ProtocolModel):
    """Strict response returned by the authenticated Worker upload."""

    batch_id: str = Field(alias="batchId", min_length=1)
    kind: ObjectKind
    sha256: Sha256
    status: Literal["linked"]


class IngestionPublication(ProtocolModel):
    """A non-tombstone item in the server ingestion contract."""

    source_id: SourceId = Field(alias="sourceId")
    canonical_url: Url = Field(alias="canonicalUrl")
    revision_hash: Sha256 = Field(alias="revisionHash")
    observed_at: str = Field(alias="observedAt", min_length=1)
    tombstone: Literal[False] = False
    publication_type: PublicationType = Field(alias="publicationType")
    title: TrimmedText = Field(min_length=1, max_length=MAX_PUBLICATION_TITLE_LENGTH)
    author: TrimmedOptionalText = Field(default=None, max_length=MAX_PUBLICATION_AUTHOR_LENGTH)
    published_at: str | None = Field(default=None, alias="publishedAt")
    updated_at_source: str | None = Field(default=None, alias="updatedAtSource")
    category: TrimmedOptionalText = Field(default=None, max_length=MAX_PUBLICATION_CATEGORY_LENGTH)
    summary: TrimmedOptionalText = Field(default=None, max_length=MAX_PUBLICATION_SUMMARY_LENGTH)
    body_text: SanitizedOptionalText = Field(
        default=None,
        alias="bodyText",
        max_length=MAX_PUBLICATION_BODY_TEXT_LENGTH,
    )
    source_page_url: Url | None = Field(default=None, alias="sourcePageUrl")
    extraction_method: TrimmedOptionalText = Field(
        default=None,
        alias="extractionMethod",
        max_length=MAX_PUBLICATION_EXTRACTION_METHOD_LENGTH,
    )
    classifier_version: TrimmedOptionalText = Field(
        default=None,
        alias="classifierVersion",
        max_length=200,
    )
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
    _normalize_raw_metadata = field_validator("raw_metadata", mode="before")(_json_value)


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

    protocol_version: Literal["1"] = Field(
        default=INGESTION_PROTOCOL_VERSION, alias="protocolVersion"
    )
    producer_version: TrimmedText = Field(alias="producerVersion", min_length=1, max_length=200)
    client_run_id: TrimmedText = Field(alias="clientRunId", min_length=1, max_length=200)
    batch_id: TrimmedText = Field(alias="batchId", min_length=1, max_length=200)
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


def _normalized_source_page_url(article: ArticleDocument) -> str:
    source_page_url = sanitize_text(article.source_page_url or "").strip()
    return source_page_url or sanitize_text(article.url).strip()


def _normalized_publication_values(
    article: ArticleDocument,
    *,
    publication_type: PublicationType | None,
    classifier_version: str,
    objects: list[ObjectManifest] | tuple[ObjectManifest, ...],
) -> dict[str, Any]:
    """Return one normalized representation shared by hashing and the wire model."""

    source_id = sanitize_text(article.source_id).strip()
    canonical_url = sanitize_text(article.url).strip()
    source_page_url = _normalized_source_page_url(article)
    title = _bounded_required_text(article.title, MAX_PUBLICATION_TITLE_LENGTH)
    author = _bounded_optional_text(article.author, MAX_PUBLICATION_AUTHOR_LENGTH)
    category = _bounded_optional_text(article.category, MAX_PUBLICATION_CATEGORY_LENGTH)
    summary = _bounded_optional_text(article.summary, MAX_PUBLICATION_SUMMARY_LENGTH)
    kind = publication_type or classify_publication(
        url=canonical_url,
        source_id=source_id,
        title=title,
        category=category or "",
        source_page_url=source_page_url,
    )
    return {
        "sourceId": source_id,
        "canonicalUrl": canonical_url,
        "title": title,
        "author": author,
        "publishedAt": _wire_timestamp(article.published_at),
        "updatedAtSource": _wire_timestamp(article.updated_at),
        "category": category,
        "summary": summary,
        "bodyText": _bounded_body_text(article.body_text, MAX_PUBLICATION_BODY_TEXT_LENGTH),
        "sourcePageUrl": source_page_url,
        "extractionMethod": _bounded_optional_text(
            article.extraction_method,
            MAX_PUBLICATION_EXTRACTION_METHOD_LENGTH,
        ),
        "classifierVersion": _bounded_optional_text(classifier_version, 200),
        "publicationType": kind,
        "rawMetadata": _json_value(article.raw_metadata),
        "objects": _bounded_objects(objects),
    }


def _wire_publication_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        **values,
        "objects": [
            manifest.model_dump(by_alias=True, mode="json", exclude_none=True)
            for manifest in values["objects"]
        ],
    }


def _revision_hash_for_values(values: dict[str, Any]) -> str:
    revision_values = {
        **values,
        "objects": sorted(
            values["objects"],
            key=lambda item: (
                item.kind,
                item.sort_order if item.sort_order is not None else -1,
                item.sha256,
            ),
        ),
    }
    encoded = json.dumps(
        _wire_publication_values(revision_values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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

    values = _normalized_publication_values(
        article,
        publication_type=publication_type,
        classifier_version=classifier_version,
        objects=objects,
    )
    return _revision_hash_for_values(values)


def build_publication(
    article: ArticleDocument,
    *,
    objects: list[ObjectManifest] | tuple[ObjectManifest, ...] = (),
    publication_type: PublicationType | None = None,
    classifier_version: str = CLASSIFIER_VERSION,
    observed_at: str | date | datetime | None = None,
) -> IngestionPublication:
    values = _normalized_publication_values(
        article,
        publication_type=publication_type,
        classifier_version=classifier_version,
        objects=objects,
    )
    revision_hash = _revision_hash_for_values(values)
    observation = observed_at or datetime.now(SHANGHAI)
    return IngestionPublication(
        sourceId=values["sourceId"],
        canonicalUrl=values["canonicalUrl"],
        revisionHash=revision_hash,
        observedAt=normalize_publication_timestamp(observation),
        publicationType=values["publicationType"],
        title=values["title"],
        author=values["author"],
        publishedAt=values["publishedAt"],
        updatedAtSource=values["updatedAtSource"],
        category=values["category"],
        summary=values["summary"],
        bodyText=values["bodyText"],
        sourcePageUrl=values["sourcePageUrl"],
        extractionMethod=values["extractionMethod"],
        classifierVersion=values["classifierVersion"],
        rawMetadata=values["rawMetadata"] or None,
        objects=values["objects"],
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
    "PublicationObjectPlanItem",
    "PublicationObjectPlanRequestItem",
    "PublicationObjectPlanRequest",
    "PublicationObjectPlanResponse",
    "PublicationObjectUploadResponse",
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
