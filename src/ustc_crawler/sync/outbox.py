from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db.engine import Database
from ..db.models import SyncBatch, SyncBatchItem, SyncOutbox, SyncRun
from ..db.uow import transaction
from ..models import ArticleDocument, sanitize_text
from .models import (
    INGESTION_PROTOCOL_VERSION,
    MAX_PUBLICATION_BATCH_ITEMS,
    MAX_PUBLICATION_OBJECTS,
    IngestionBatch,
    IngestionPublication,
    LocalObjectManifest,
    ObjectKind,
    ObjectManifest,
    PublicationItem,
    PublicationSourceDescriptor,
    TombstonePublication,
    build_ingestion_batch,
    build_publication,
)

type LocalObjectInput = str | Path | tuple[str | Path, str]
MAX_OBJECT_SIZE = 32 * 1024 * 1024
DEFAULT_MAX_BATCH_BYTES = 2 * 1024 * 1024
BATCH_STATUSES = frozenset(
    {"pending", "uploading", "acked", "partial", "failed", "superseded"}
)
RELEASABLE_ITEM_STATUSES = frozenset({"pending", "uploading", "failed"})
RELEASABLE_OUTBOX_STATUSES = frozenset({"pending", "batched", "uploading", "failed"})


def _release_batch_events(
    session: Session,
    batch: SyncBatch,
    *,
    error: str,
) -> tuple[list[SyncBatchItem], list[SyncOutbox]]:
    """Supersede a batch and return its events to the pending outbox.

    The batch and item rows stay as an immutable audit record; only events
    whose membership and unfinished statuses validate exactly are released.
    """

    batch_items = list(
        session.scalars(
            select(SyncBatchItem)
            .where(SyncBatchItem.batch_id == batch.id)
            .order_by(SyncBatchItem.item_key)
        ).all()
    )
    outbox_rows = list(
        session.scalars(
            select(SyncOutbox)
            .where(SyncOutbox.batch_id == batch.id)
            .order_by(SyncOutbox.event_id)
        ).all()
    )
    if {item.item_key for item in batch_items} != {row.event_id for row in outbox_rows}:
        raise ValueError("batch item and outbox membership mismatch")
    if any(item.status not in RELEASABLE_ITEM_STATUSES for item in batch_items):
        raise ValueError("batch contains completed or rejected items")
    if any(row.status not in RELEASABLE_OUTBOX_STATUSES for row in outbox_rows):
        raise ValueError("batch contains completed outbox events")

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    batch.status = "superseded"
    batch.next_attempt_at = None
    batch.locked_until = None
    batch.last_error = error
    batch.updated_at = now
    for row in outbox_rows:
        row.batch_id = None
        row.status = "pending"
        row.next_attempt_at = None
        row.locked_until = None
        row.response_json = None
        row.last_error = None
        row.updated_at = now
    return batch_items, outbox_rows


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
                sanitize_text(article.body_html).encode("utf-8"),
                kind="body_html",
                content_type="text/html",
            )
        )
    if article.body_markdown:
        objects.append(
            spool_bytes(
                data_dir,
                sanitize_text(article.body_markdown).encode("utf-8"),
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
    return tuple(objects[:MAX_PUBLICATION_OBJECTS])


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class IngestionOutbox:
    """Durable immutable revision events and deterministic batch construction."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _ensure_ingestion_source(source: PublicationSourceDescriptor) -> None:
        if source.discovery_only:
            raise ValueError("discovery-only source cannot enqueue publications")

    @staticmethod
    def _event_id(publication: PublicationItem) -> str:
        value = f"{publication.source_id}\n{publication.canonical_url}\n{publication.revision_hash}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def enqueue_publication_in_session(
        self,
        session: Session,
        publication: PublicationItem,
        *,
        source: PublicationSourceDescriptor,
        local_objects: Iterable[LocalObjectManifest] = (),
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        """Insert one immutable revision event and return its stable event id."""

        if source.id != publication.source_id:
            raise ValueError("source descriptor does not match publication sourceId")
        self._ensure_ingestion_source(source)
        payload_json = _dump_json(publication.model_dump(by_alias=True, mode="json", exclude_none=True))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        manifests = tuple(local_objects)
        if isinstance(publication, IngestionPublication):
            wire_objects = [wire_manifest(manifest) for manifest in manifests]
            if wire_objects != publication.objects:
                raise ValueError("local object manifests do not match publication objects")
        elif isinstance(publication, TombstonePublication):
            if manifests:
                raise ValueError("tombstone publication cannot have local object manifests")
        else:
            raise TypeError(f"unsupported publication item: {type(publication).__name__}")
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
            try:
                existing_payload = json.loads(existing.payload_json)
                current_payload = json.loads(payload_json)
                existing_payload.pop("observedAt", None)
                current_payload.pop("observedAt", None)
                existing_payload_digest = hashlib.sha256(
                    existing.payload_json.encode("utf-8")
                ).hexdigest()
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError(f"invalid immutable outbox event: {event_id}") from exc
            if (
                existing.payload_sha256 != existing_payload_digest
                or existing_payload != current_payload
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
        publication: PublicationItem,
        *,
        source: PublicationSourceDescriptor,
        local_objects: Iterable[LocalObjectManifest] = (),
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        return self.enqueue_publication_with_status(
            publication,
            source=source,
            local_objects=local_objects,
            run_id=run_id,
            created_at=created_at,
        )[0]

    def enqueue_publication_with_status(
        self,
        publication: PublicationItem,
        *,
        source: PublicationSourceDescriptor,
        local_objects: Iterable[LocalObjectManifest] = (),
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> tuple[str, bool]:
        """Insert an event and report whether this call created its row."""

        self._ensure_ingestion_source(source)
        with transaction(self.database) as session:
            event_id = self._event_id(publication)
            created = session.get(SyncOutbox, event_id) is None
            event_id = self.enqueue_publication_in_session(
                session,
                publication,
                source=source,
                local_objects=local_objects,
                run_id=run_id,
                created_at=created_at,
            )
            return event_id, created

    def delete_unbatched_discovery_only_events(self) -> int:
        """Delete legacy discovery-only events that were never batched.

        Once an event is assigned to a batch, it is part of the immutable
        batch audit trail and is deliberately left untouched.
        """

        with transaction(self.database) as session:
            rows = session.scalars(
                select(SyncOutbox).where(SyncOutbox.batch_id.is_(None))
            ).all()
            deleted = 0
            for row in rows:
                if self._source(row).discovery_only:
                    session.delete(row)
                    deleted += 1
            return deleted

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
        return self.enqueue_article_with_status(
            article,
            data_dir,
            source=source,
            media_paths=media_paths,
            asset_paths=asset_paths,
            run_id=run_id,
            observed_at=observed_at,
        )[0]

    def enqueue_article_with_status(
        self,
        article: ArticleDocument,
        data_dir: str | Path,
        *,
        source: PublicationSourceDescriptor,
        media_paths: dict[str, LocalObjectInput] | None = None,
        asset_paths: dict[str, LocalObjectInput] | None = None,
        run_id: str | None = None,
        observed_at: str | date | datetime | None = None,
    ) -> tuple[str, bool]:
        """Snapshot an article and report whether its immutable event was new."""

        self._ensure_ingestion_source(source)
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
        return self.enqueue_publication_with_status(
            publication,
            source=source,
            local_objects=local_objects,
            run_id=run_id,
        )

    @staticmethod
    def _publication(row: SyncOutbox) -> PublicationItem:
        value = json.loads(row.payload_json)
        if isinstance(value, dict) and value.get("tombstone") is True:
            parsed: PublicationItem = TombstonePublication.model_validate(value)
        else:
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

    def pending_batches(self) -> list[SyncBatch]:
        """Return batches that need delivery, oldest first."""

        with self.database.session_factory() as session:
            return session.scalars(
                select(SyncBatch)
                .where(SyncBatch.status.in_(("pending", "uploading")))
                .order_by(SyncBatch.created_at, SyncBatch.id)
            ).all()

    def batch_objects(
        self,
        batch_id: str,
        *,
        item_keys: Iterable[str] | None = None,
    ) -> tuple[LocalObjectManifest, ...]:
        """Load immutable manifests, optionally only for selected item keys."""

        with self.database.session_factory() as session:
            if item_keys is None:
                item_query = select(SyncBatchItem.item_key).where(
                    SyncBatchItem.batch_id == batch_id
                )
                event_ids = set(session.scalars(item_query).all())
            else:
                event_ids = set(item_keys)
                if not event_ids:
                    return ()
            if not event_ids:
                return ()
            rows = session.scalars(
                select(SyncOutbox)
                .where(
                    SyncOutbox.batch_id == batch_id,
                    SyncOutbox.event_id.in_(event_ids),
                )
                .order_by(SyncOutbox.created_at, SyncOutbox.event_id)
            ).all()
            manifests: list[LocalObjectManifest] = []
            for row in rows:
                values = json.loads(row.object_manifest_json)
                if not isinstance(values, list):
                    raise ValueError(f"invalid object manifest for outbox event: {row.event_id}")
                manifests.extend(LocalObjectManifest.model_validate(value) for value in values)
            return tuple(manifests)

    def batch_item_keys(
        self,
        batch_id: str,
        identities: Iterable[tuple[str, str, str]],
    ) -> set[str]:
        """Resolve server item identities to immutable local event keys."""

        requested = set(identities)
        if not requested:
            return set()
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(SyncBatchItem).where(SyncBatchItem.batch_id == batch_id)
            ).all()
            resolved = {
                (row.source_id, row.canonical_url, row.revision_hash): row.item_key for row in rows
            }
        if not requested.issubset(resolved):
            raise ValueError("batch item identity does not match persisted items")
        return {resolved[identity] for identity in requested}

    def mark_batch_results(
        self,
        batch_id: str,
        *,
        accepted_identities: Iterable[tuple[str, str, str]],
        rejected_identities: Iterable[tuple[str, str, str]],
        response_json: str,
    ) -> str:
        """Persist per-item outcomes after the server accepted the batch.

        The server response identifies items by source, canonical URL, and
        revision hash.  Rejected item descriptions are deliberately reduced
        to ``server_rejected`` so an arbitrary response body cannot become
        durable local data.
        """

        accepted = set(accepted_identities)
        rejected = set(rejected_identities)
        if accepted & rejected:
            raise ValueError("an item cannot be both accepted and rejected")
        if not response_json:
            raise ValueError("batch response cannot be empty")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with transaction(self.database) as session:
            batch = session.get(SyncBatch, batch_id)
            if batch is None:
                raise KeyError(batch_id)
            batch_items = session.scalars(
                select(SyncBatchItem)
                .where(SyncBatchItem.batch_id == batch_id)
                .order_by(SyncBatchItem.item_key)
            ).all()
            expected = {
                (item.source_id, item.canonical_url, item.revision_hash)
                for item in batch_items
            }
            if expected != accepted | rejected:
                raise ValueError("batch result membership does not match persisted items")
            for item in batch_items:
                row = session.get(SyncOutbox, item.item_key)
                if row is None:
                    raise ValueError(f"missing outbox event for batch item: {item.item_key}")
                identity = (item.source_id, item.canonical_url, item.revision_hash)
                if identity in accepted:
                    item.status = "acked"
                    item.error = None
                    row.status = "acked"
                    row.last_error = None
                else:
                    item.status = "rejected"
                    item.error = "server_rejected"
                    row.status = "failed"
                    row.last_error = "server_rejected"
                row.response_json = response_json
                row.updated_at = now
            batch.status = (
                "partial" if accepted and rejected else "failed" if rejected else "acked"
            )
            batch.response_json = response_json
            batch.last_error = "server_rejected" if rejected else None
            batch.updated_at = now
            return batch.status

    def mark_batch_error(self, batch_id: str, error_code: str) -> None:
        """Record a safe error code without persisting response bodies or URLs."""

        if not error_code or any(character.isspace() for character in error_code):
            raise ValueError("batch error must be a non-empty code without whitespace")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with transaction(self.database) as session:
            batch = session.get(SyncBatch, batch_id)
            if batch is None:
                raise KeyError(batch_id)
            batch.last_error = error_code
            batch.attempts += 1
            batch.updated_at = now
            for row in session.scalars(select(SyncOutbox).where(SyncOutbox.batch_id == batch_id)):
                row.last_error = error_code
                row.updated_at = now

    @staticmethod
    def _coalesce_pending_events(session: Session) -> int:
        """Supersede all but the newest pending event for each entity.

        Only the newest pending revision of an article is worth syncing: its
        content subsumes every older pending revision, and delivering an
        older revision after the newer one could roll the article back.
        Superseded events keep their immutable payload; only lifecycle
        metadata changes, mirroring the existing batch-supersede semantics.
        Tombstones share the identity key with body events, so a newest
        tombstone is kept (the deletion must be delivered) and only yields
        when an even newer body event exists.
        """

        rows = session.execute(
            select(SyncOutbox.event_id, SyncOutbox.entity_key, SyncOutbox.created_at)
            .where(SyncOutbox.status == "pending", SyncOutbox.batch_id.is_(None))
            .order_by(SyncOutbox.entity_key, SyncOutbox.created_at, SyncOutbox.event_id)
        ).all()
        newest: dict[str, tuple[str, str]] = {}
        for event_id, entity_key, created_at in rows:
            rank = (created_at, event_id)
            if entity_key not in newest or rank > newest[entity_key]:
                newest[entity_key] = rank
        stale = [
            event_id
            for event_id, entity_key, created_at in rows
            if (created_at, event_id) != newest[entity_key]
        ]
        if not stale:
            return 0
        session.execute(
            update(SyncOutbox)
            .where(SyncOutbox.event_id.in_(stale))
            .values(
                status="superseded",
                last_error="coalesced_by_newer",
                updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        )
        return len(stale)

    def build_batch(
        self,
        *,
        run_id: str,
        batch_id: str,
        producer_version: str,
        observed_at: str | date | datetime,
        limit: int = 50,
        max_payload_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    ) -> IngestionBatch | None:
        """Claim pending events and persist their exact immutable batch body."""

        if limit < 1:
            raise ValueError("batch limit must be positive")
        if limit > MAX_PUBLICATION_BATCH_ITEMS:
            raise ValueError(
                "batch limit must be between 1 and "
                f"{MAX_PUBLICATION_BATCH_ITEMS}"
            )
        if max_payload_bytes < 1:
            raise ValueError("max payload bytes must be positive")
        if max_payload_bytes > DEFAULT_MAX_BATCH_BYTES:
            raise ValueError(f"max payload bytes cannot exceed {DEFAULT_MAX_BATCH_BYTES}")
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

            self._coalesce_pending_events(session)
            candidate_rows = session.scalars(
                select(SyncOutbox)
                .where(SyncOutbox.status == "pending", SyncOutbox.batch_id.is_(None))
                .order_by(SyncOutbox.created_at.desc(), SyncOutbox.event_id)
                .limit(limit)
            ).all()
            if not candidate_rows:
                return None
            rows: list[SyncOutbox] = []
            publications: list[PublicationItem] = []
            for candidate in candidate_rows:
                candidate_rows_for_batch = [*rows, candidate]
                candidate_publications = [*publications, self._publication(candidate)]
                candidate_batch = build_ingestion_batch(
                    candidate_publications,
                    sources=self._sources(candidate_rows_for_batch),
                    client_run_id=run_id,
                    batch_id=batch_id,
                    observed_at=observed_at,
                    producer_version=producer_version,
                )
                if len(candidate_batch.payload_bytes()) > max_payload_bytes:
                    if not rows:
                        # Poison pill isolation: an event that can never fit
                        # an empty batch would wedge the queue forever, so
                        # fail it terminally and skip to the next candidate.
                        candidate.status = "failed"
                        candidate.last_error = "event_too_large"
                        candidate.updated_at = (
                            datetime.now().astimezone().isoformat(timespec="seconds")
                        )
                        continue
                    break
                rows = candidate_rows_for_batch
                publications = candidate_publications
            if not rows:
                return None
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

    def recover_oversized_batch(self, batch_id: str) -> int:
        """Release an oversized in-flight batch back to the pending outbox.

        The original batch and item rows remain as an immutable audit record.
        Only a pending, uploading, or failed batch whose item rows are all
        still unfinished can be released.  A superseded batch cannot be
        released twice, and completed or partially completed batches are
        never rewritten.
        """

        recoverable_batch_statuses = {"failed", "uploading", "pending"}
        with transaction(self.database) as session:
            batch = session.get(SyncBatch, batch_id)
            if batch is None:
                raise KeyError(batch_id)
            if batch.status not in recoverable_batch_statuses:
                raise ValueError(f"batch status cannot be recovered: {batch.status}")

            item_count = session.scalar(
                select(func.count())
                .select_from(SyncBatchItem)
                .where(SyncBatchItem.batch_id == batch_id)
            )
            if item_count is None or item_count <= MAX_PUBLICATION_BATCH_ITEMS:
                raise ValueError(
                    "batch is not oversized: "
                    f"{item_count} items (maximum {MAX_PUBLICATION_BATCH_ITEMS})"
                )

            batch_items, outbox_rows = _release_batch_events(
                session,
                batch,
                error="oversized_batch_superseded",
            )

            outbox_by_id = {row.event_id: row for row in outbox_rows}
            for item in batch_items:
                row = outbox_by_id[item.item_key]
                publication = self._publication(row)
                if (
                    row.revision_hash != item.revision_hash
                    or publication.source_id != item.source_id
                    or publication.canonical_url != item.canonical_url
                    or publication.revision_hash != item.revision_hash
                ):
                    raise ValueError("batch item and outbox identity mismatch")
            return len(outbox_rows)

    def requeue_failed_batches(
        self,
        *,
        errors: set[str],
        max_attempts: int = 5,
        force: bool = False,
    ) -> dict[str, int]:
        """Release events from locally-failed batches back to the pending outbox.

        Batches that failed for local reasons (e.g. ``immutable_object_changed``
        when the runner had no spool files) never reached the server, so their
        events can be redelivered safely: the server deduplicates by revision
        hash.  Batches the server itself rejected keep their terminal state.
        Batches whose delivery already failed ``max_attempts`` times stay
        terminal unless ``force`` is set.  The original batch and item rows
        remain as an immutable audit record.
        """

        if max_attempts < 1:
            raise ValueError("max attempts must be positive")
        result = {"batches": 0, "events": 0}
        with transaction(self.database) as session:
            query = select(SyncBatch).where(
                SyncBatch.status == "failed", SyncBatch.last_error.in_(errors)
            )
            if not force:
                query = query.where(SyncBatch.attempts < max_attempts)
            batches = session.scalars(query).all()
            for batch in batches:
                _batch_items, outbox_rows = _release_batch_events(
                    session,
                    batch,
                    error=f"requeued_failed_batch:{batch.last_error}",
                )
                result["batches"] += 1
                result["events"] += len(outbox_rows)
        return result

    def mark_batch(self, batch_id: str, *, status: str, response_json: str = "") -> None:
        if status not in BATCH_STATUSES:
            raise ValueError(f"unknown batch status: {status}")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with transaction(self.database) as session:
            batch = session.get(SyncBatch, batch_id)
            if batch is None:
                raise KeyError(batch_id)
            batch.status = status
            batch.response_json = response_json or None
            if status == "acked":
                batch.last_error = None
            batch.updated_at = now
            for row in session.scalars(select(SyncOutbox).where(SyncOutbox.batch_id == batch_id)):
                row.status = status
                row.response_json = response_json or None
                if status == "acked":
                    row.last_error = None
                row.updated_at = now
            for row in session.scalars(
                select(SyncBatchItem).where(SyncBatchItem.batch_id == batch_id)
            ):
                row.status = status


__all__ = [
    "DEFAULT_MAX_BATCH_BYTES",
    "IngestionOutbox",
    "spool_article_objects",
    "spool_bytes",
    "spool_file",
    "wire_manifest",
]
