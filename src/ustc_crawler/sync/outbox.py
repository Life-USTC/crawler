from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.engine import Database
from ..db.models import SyncBatch, SyncBatchItem, SyncOutbox, SyncRun
from ..db.uow import transaction
from ..models import ArticleDocument
from .models import (
    INGESTION_PROTOCOL_VERSION,
    IngestionBatch,
    IngestionPublication,
    LocalObjectManifest,
    ObjectKind,
    ObjectManifest,
    PublicationSourceDescriptor,
    build_ingestion_batch,
    build_publication,
)

type LocalObjectInput = str | Path | tuple[str | Path, str]
MAX_OBJECT_SIZE = 32 * 1024 * 1024


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _content_type(value: str) -> str:
    # Server validation intentionally rejects parameters such as charset=utf-8.
    return value.split(";", 1)[0].strip().lower()


def spool_bytes(
    data_dir: str | Path,
    body: bytes,
    *,
    kind: ObjectKind,
    content_type: str,
    sort_order: int | None = None,
    alt_text: str | None = None,
) -> LocalObjectManifest:
    """Atomically persist bytes in the local content-addressed sync spool."""

    if len(body) > MAX_OBJECT_SIZE:
        raise ValueError(f"object exceeds server limit of {MAX_OBJECT_SIZE} bytes")
    digest = hashlib.sha256(body).hexdigest()
    root = Path(data_dir) / "sync-objects" / "sha256" / digest[:2]
    root.mkdir(parents=True, exist_ok=True)
    target = root / digest
    if target.exists():
        saved_digest, saved_size = _sha256_file(target)
        if saved_digest != digest or saved_size != len(body):
            raise RuntimeError(f"content-addressed spool collision: {target}")
    else:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return LocalObjectManifest(
        kind=kind,
        sha256=digest,
        size=len(body),
        contentType=_content_type(content_type),
        sortOrder=sort_order,
        altText=alt_text,
        local_path=str(target),
    )


def spool_file(
    path: str | Path,
    *,
    kind: ObjectKind,
    content_type: str,
    sort_order: int | None = None,
    alt_text: str | None = None,
) -> LocalObjectManifest:
    """Validate an existing file and create its immutable object manifest."""

    target = Path(path)
    digest, size = _sha256_file(target)
    if size > MAX_OBJECT_SIZE:
        raise ValueError(f"object exceeds server limit of {MAX_OBJECT_SIZE} bytes")
    return LocalObjectManifest(
        kind=kind,
        sha256=digest,
        size=size,
        contentType=_content_type(content_type),
        sortOrder=sort_order,
        altText=alt_text,
        local_path=str(target),
    )


def wire_manifest(manifest: LocalObjectManifest) -> ObjectManifest:
    """Drop local-only paths before a manifest enters an HTTP payload."""

    return ObjectManifest.model_validate(manifest.model_dump(exclude={"local_path"}))


def _media_input(value: LocalObjectInput) -> tuple[Path, str]:
    if isinstance(value, tuple):
        path, content_type = value
        return Path(path), content_type
    return Path(value), "application/octet-stream"


def spool_article_objects(
    article: ArticleDocument,
    data_dir: str | Path,
    *,
    media_paths: dict[str, LocalObjectInput] | None = None,
    asset_paths: dict[str, LocalObjectInput] | None = None,
) -> tuple[LocalObjectManifest, ...]:
    """Create immutable HTML/Markdown/media/asset objects for one article snapshot."""

    objects: list[LocalObjectManifest] = []
    if article.body_html:
        objects.append(
            spool_bytes(
                data_dir,
                article.body_html.encode("utf-8"),
                kind="body_html",
                content_type="text/html",
            )
        )
    if article.body_markdown:
        objects.append(
            spool_bytes(
                data_dir,
                article.body_markdown.encode("utf-8"),
                kind="body_markdown",
                content_type="text/markdown",
            )
        )
    seen_media: set[tuple[str, str]] = set()
    for sort_order, image in enumerate(article.images):
        value = (media_paths or {}).get(image.url)
        if value is None:
            continue
        path, content_type = _media_input(value)
        if not path.is_file():
            continue
        manifest = spool_file(
            path,
            kind="media",
            content_type=content_type,
            sort_order=sort_order,
            alt_text=image.alt or None,
        )
        key = (manifest.kind, manifest.sha256)
        if key not in seen_media:
            seen_media.add(key)
            objects.append(manifest)
    seen_assets: set[str] = set()
    for asset_url, value in sorted((asset_paths or {}).items()):
        path, content_type = _media_input(value)
        if not path.is_file():
            continue
        manifest = spool_file(path, kind="asset", content_type=content_type)
        if manifest.sha256 not in seen_assets:
            seen_assets.add(manifest.sha256)
            objects.append(manifest)
    return tuple(objects)


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class IngestionOutbox:
    """Durable immutable revision events and deterministic batch construction."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _event_id(publication: IngestionPublication) -> str:
        value = f"{publication.source_id}\n{publication.canonical_url}\n{publication.revision_hash}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def enqueue_publication_in_session(
        self,
        session: Session,
        publication: IngestionPublication,
        *,
        source: PublicationSourceDescriptor,
        local_objects: Iterable[LocalObjectManifest] = (),
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        """Insert one immutable revision event and return its stable event id."""

        if source.id != publication.source_id:
            raise ValueError("source descriptor does not match publication sourceId")
        payload_json = _dump_json(publication.model_dump(by_alias=True, mode="json", exclude_none=True))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        manifests = tuple(local_objects)
        wire_objects = [wire_manifest(manifest) for manifest in manifests]
        if wire_objects != publication.objects:
            raise ValueError("local object manifests do not match publication objects")
        object_manifest_json = _dump_json(
            [manifest.model_dump(mode="json") for manifest in manifests]
        )
        source_json = _dump_json(source.model_dump(by_alias=True, mode="json", exclude_none=True))
        event_id = self._event_id(publication)
        now = created_at or datetime.now().astimezone().isoformat(timespec="seconds")
        if run_id is not None and session.get(SyncRun, run_id) is None:
            raise ValueError(f"sync run does not exist: {run_id}")
        existing = session.get(SyncOutbox, event_id)
        if existing is not None:
            if (
                existing.payload_sha256 != payload_sha256
                or existing.object_manifest_json != object_manifest_json
                or existing.source_json != source_json
            ):
                raise ValueError(f"immutable outbox event changed: {event_id}")
            return event_id
        session.add(
            SyncOutbox(
                event_id=event_id,
                entity_key=f"{publication.source_id}:{publication.canonical_url}",
                revision_hash=publication.revision_hash,
                run_id=run_id,
                payload_json=payload_json,
                payload_sha256=payload_sha256,
                source_json=source_json,
                object_manifest_json=object_manifest_json,
                status="pending",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
        return event_id

    def enqueue_publication(
        self,
        publication: IngestionPublication,
        *,
        source: PublicationSourceDescriptor,
        local_objects: Iterable[LocalObjectManifest] = (),
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        with transaction(self.database) as session:
            return self.enqueue_publication_in_session(
                session,
                publication,
                source=source,
                local_objects=local_objects,
                run_id=run_id,
                created_at=created_at,
            )

    def enqueue_article(
        self,
        article: ArticleDocument,
        data_dir: str | Path,
        *,
        source: PublicationSourceDescriptor,
        media_paths: dict[str, LocalObjectInput] | None = None,
        asset_paths: dict[str, LocalObjectInput] | None = None,
        run_id: str | None = None,
        observed_at: str | date | datetime | None = None,
    ) -> str:
        local_objects = spool_article_objects(
            article,
            data_dir,
            media_paths=media_paths,
            asset_paths=asset_paths,
        )
        wire_objects = [wire_manifest(item) for item in local_objects]
        publication = build_publication(
            article,
            objects=wire_objects,
            observed_at=observed_at,
        )
        return self.enqueue_publication(
            publication,
            source=source,
            local_objects=local_objects,
            run_id=run_id,
        )

    @staticmethod
    def _publication(row: SyncOutbox) -> IngestionPublication:
        value = json.loads(row.payload_json)
        parsed = IngestionPublication.model_validate(value)
        if parsed.revision_hash != row.revision_hash:
            raise ValueError(f"outbox revision hash mismatch: {row.event_id}")
        return parsed

    @staticmethod
    def _source(row: SyncOutbox) -> PublicationSourceDescriptor:
        return PublicationSourceDescriptor.model_validate(json.loads(row.source_json))

    @staticmethod
    def _sources(rows: Iterable[SyncOutbox]) -> list[PublicationSourceDescriptor]:
        unique: dict[str, PublicationSourceDescriptor] = {}
        for row in rows:
            source = IngestionOutbox._source(row)
            previous = unique.get(source.id)
            if previous is not None and previous != source:
                raise ValueError(f"source descriptor changed within pending outbox: {source.id}")
            unique[source.id] = source
        return [unique[key] for key in sorted(unique)]

    @staticmethod
    def _stored_batch_sources(row: SyncBatch) -> list[PublicationSourceDescriptor]:
        return [
            PublicationSourceDescriptor.model_validate(value)
            for value in json.loads(row.sources_json)
        ]

    def build_batch(
        self,
        *,
        run_id: str,
        batch_id: str,
        producer_version: str,
        observed_at: str | date | datetime,
        limit: int = 50,
    ) -> IngestionBatch | None:
        """Claim pending events and persist their exact immutable batch body."""

        if limit < 1:
            raise ValueError("batch limit must be positive")
        with transaction(self.database) as session:
            existing = session.get(SyncBatch, batch_id)
            if existing is not None:
                rows = session.scalars(
                    select(SyncOutbox)
                    .where(SyncOutbox.batch_id == batch_id)
                    .order_by(SyncOutbox.created_at, SyncOutbox.event_id)
                ).all()
                batch = build_ingestion_batch(
                    [self._publication(row) for row in rows],
                    sources=self._stored_batch_sources(existing),
                    client_run_id=existing.client_run_id,
                    batch_id=existing.id,
                    observed_at=existing.observed_at,
                    producer_version=existing.producer_version,
                )
                if batch.payload_sha256() != existing.payload_sha256:
                    raise ValueError(f"immutable batch payload changed: {batch_id}")
                return batch

            rows = session.scalars(
                select(SyncOutbox)
                .where(SyncOutbox.status == "pending", SyncOutbox.batch_id.is_(None))
                .order_by(SyncOutbox.created_at, SyncOutbox.event_id)
                .limit(limit)
            ).all()
            if not rows:
                return None
            publications = [self._publication(row) for row in rows]
            sources = self._sources(rows)
            batch = build_ingestion_batch(
                publications,
                sources=sources,
                client_run_id=run_id,
                batch_id=batch_id,
                observed_at=observed_at,
                producer_version=producer_version,
            )
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            session.add(
                SyncBatch(
                    id=batch_id,
                    run_id=run_id if session.get(SyncRun, run_id) is not None else None,
                    client_run_id=run_id,
                    sources_json=_dump_json(batch.payload_dict()["sources"]),
                    observed_at=batch.observed_at,
                    payload_sha256=batch.payload_sha256(),
                    protocol_version=INGESTION_PROTOCOL_VERSION,
                    producer_version=producer_version,
                    status="pending",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            for row, publication in zip(rows, publications, strict=True):
                row.batch_id = batch_id
                row.status = "batched"
                row.updated_at = now
                session.add(
                    SyncBatchItem(
                        batch_id=batch_id,
                        item_key=row.event_id,
                        source_id=publication.source_id,
                        canonical_url=publication.canonical_url,
                        revision_hash=publication.revision_hash,
                        status="pending",
                    )
                )
            return batch

    def mark_batch(self, batch_id: str, *, status: str, response_json: str = "") -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with transaction(self.database) as session:
            batch = session.get(SyncBatch, batch_id)
            if batch is None:
                raise KeyError(batch_id)
            batch.status = status
            batch.response_json = response_json or None
            batch.updated_at = now
            for row in session.scalars(select(SyncOutbox).where(SyncOutbox.batch_id == batch_id)):
                row.status = status
                row.response_json = response_json or None
                row.updated_at = now
            for row in session.scalars(
                select(SyncBatchItem).where(SyncBatchItem.batch_id == batch_id)
            ):
                row.status = status


__all__ = [
    "IngestionOutbox",
    "spool_article_objects",
    "spool_bytes",
    "spool_file",
    "wire_manifest",
]
