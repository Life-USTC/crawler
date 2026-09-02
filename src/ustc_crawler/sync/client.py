"""HTTPX client for durable publication batch replay and object uploads."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from .. import __version__
from ..db.models import SyncBatch, SyncRun
from .models import (
    MAX_OBJECT_PLAN_OBJECTS,
    MAX_PUBLICATION_BATCH_ITEMS,
    IngestionBatch,
    IngestionBatchResponse,
    LocalObjectManifest,
    PublicationObjectCompleteRequest,
    PublicationObjectCompleteResponse,
    PublicationObjectPlanItem,
    PublicationObjectPlanRequest,
    PublicationObjectPlanRequestItem,
    PublicationObjectPlanResponse,
)
from .outbox import (
    DEFAULT_MAX_BATCH_BYTES,
    BatchTooLargeError,
    IngestionOutbox,
)

if TYPE_CHECKING:
    from ..store import Store

BATCH_ENDPOINT = "/api/ingestion/publications/batches"
OBJECT_PLAN_ENDPOINT = "/api/ingestion/publications/objects/plan"
OBJECT_COMPLETE_ENDPOINT = "/api/ingestion/publications/objects/complete"
INGESTION_SECRET_ENV = "USTC_CRAWLER_INGESTION_SECRET"
INGESTION_SECRET_HEADER = "X-Publication-Ingestion-Secret"
RETRY_STATUS_CODES = frozenset({408, 429})
DEFAULT_OBJECT_CONCURRENCY = 8
MAX_OBJECT_CONCURRENCY = 64
DEFAULT_BATCH_CONCURRENCY = 1
MAX_BATCH_CONCURRENCY = 16
DEFAULT_HTTP_TIMEOUT = 60.0
SAFE_SERVER_ERROR_CODES = frozenset(
    {
        "bad_request",
        "conflict",
        "forbidden",
        "insufficient_scope",
        "invalid_batch",
        "invalid_request",
        "not_found",
        "publication_ingestion_bad_request",
        "publication_ingestion_conflict",
        "publication_ingestion_forbidden",
        "publication_object_bad_request",
        "publication_object_not_found",
        "publication_object_storage_unavailable",
        "unauthorized",
    }
)


def _server_base(server: str) -> str:
    value = server.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("server must be an absolute HTTP(S) URL")
    return value


class SyncClientError(RuntimeError):
    """Base class for safe, non-sensitive sync client errors."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SyncTransientError(SyncClientError):
    """A failure that can be retried by a later sync invocation."""


class SyncPermanentError(SyncClientError):
    """A response or local invariant failure that must be reported."""


class SyncProtocolError(SyncPermanentError):
    """A strict response did not match the expected server contract."""


class ImmutableObjectChangedError(SyncPermanentError):
    """A local spool file no longer matches its persisted manifest."""


def ingestion_secret_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Read the machine credential without exposing its value in errors."""

    values = os.environ if environ is None else environ
    secret = values.get(INGESTION_SECRET_ENV)
    if not isinstance(secret, str) or not secret.strip():
        raise SyncClientError("missing_ingestion_secret")
    return secret


@dataclass(frozen=True, slots=True)
class SyncOptions:
    batch_size: int = 50
    max_payload_bytes: int = DEFAULT_MAX_BATCH_BYTES
    producer_version: str = f"ustc-public-site-crawler/{__version__}"
    max_batches: int = 0
    max_retries: int = 3
    max_backoff: float = 30.0
    object_concurrency: int = DEFAULT_OBJECT_CONCURRENCY
    batch_concurrency: int = DEFAULT_BATCH_CONCURRENCY


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Durable result of one server-accepted batch."""

    status: str
    accepted: int
    rejected: int


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _error_code(response: httpx.Response) -> str:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError):
        value = None
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, str) and error in SAFE_SERVER_ERROR_CODES:
            return error
    return f"http_{response.status_code}"


def _retry_after(response: httpx.Response, now: Callable[[], float]) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.astimezone()
            return max(0.0, target.timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None


class IngestionSyncClient:
    """Replay immutable outbox batches with a machine service credential."""

    def __init__(
        self,
        database: Any,
        data_dir: str | Path,
        server: str,
        ingestion_secret: str,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(ingestion_secret, str) or not ingestion_secret.strip():
            raise ValueError("ingestion secret must not be empty")
        self.database = database
        self.data_dir = Path(data_dir)
        self.server = _server_base(server)
        self._ingestion_secret = ingestion_secret
        # Do not forward the machine secret through an unexpected redirect.
        self.http = http_client or httpx.Client(
            timeout=DEFAULT_HTTP_TIMEOUT,
            follow_redirects=False,
        )
        self._owns_http = http_client is None
        self._sleep = sleep
        self._now = now
        self.outbox = IngestionOutbox(database)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def sync(
        self,
        *,
        options: SyncOptions | None = None,
        run_id: str | None = None,
    ) -> dict[str, int | str]:
        """Deliver pending batches, replaying persisted batches before claiming new work."""

        options = options or SyncOptions()
        if options.batch_size < 1 or options.batch_size > MAX_PUBLICATION_BATCH_ITEMS:
            raise ValueError(
                "batch size must be between 1 and "
                f"{MAX_PUBLICATION_BATCH_ITEMS}"
            )
        if options.max_payload_bytes < 1:
            raise ValueError("max payload bytes must be positive")
        if options.max_payload_bytes > DEFAULT_MAX_BATCH_BYTES:
            raise ValueError(f"max payload bytes cannot exceed {DEFAULT_MAX_BATCH_BYTES}")
        if options.max_batches < 0:
            raise ValueError("max batches cannot be negative")
        if options.max_retries < 0:
            raise ValueError("max retries cannot be negative")
        if options.max_backoff < 0:
            raise ValueError("max backoff cannot be negative")
        if not 1 <= options.object_concurrency <= MAX_OBJECT_CONCURRENCY:
            raise ValueError(
                "object concurrency must be between "
                f"1 and {MAX_OBJECT_CONCURRENCY}"
            )
        if not 1 <= options.batch_concurrency <= MAX_BATCH_CONCURRENCY:
            raise ValueError(
                "batch concurrency must be between "
                f"1 and {MAX_BATCH_CONCURRENCY}"
            )
        client_run_id = run_id or uuid.uuid4().hex
        self._start_run(client_run_id)
        summary: dict[str, int | str] = {
            "runId": client_run_id,
            "batches": 0,
            "replayed": 0,
            "created": 0,
            "acked": 0,
            "failed": 0,
            "rejected": 0,
            "pending": 0,
            "items": 0,
        }
        interrupted = False
        unexpected: BaseException | None = None
        pending_replays: list[SyncBatch] = []
        replay_index = 0
        new_batches_exhausted = False
        in_flight: dict[Future[DeliveryResult], int] = {}
        sequence = 0

        def can_claim() -> bool:
            return not options.max_batches or int(summary["batches"]) < options.max_batches

        def record_claim(batch: IngestionBatch, *, replayed: bool) -> None:
            nonlocal sequence
            summary["batches"] = int(summary["batches"]) + 1
            summary["items"] = int(summary["items"]) + len(batch.items)
            if replayed:
                summary["replayed"] = int(summary["replayed"]) + 1
            else:
                summary["created"] = int(summary["created"]) + 1
            future = executor.submit(self._deliver, batch, options)
            in_flight[future] = sequence
            sequence += 1

        def process_completed(done: set[Future[DeliveryResult]]) -> None:
            nonlocal interrupted, unexpected
            for future in sorted(done, key=in_flight.__getitem__):
                in_flight.pop(future)
                try:
                    delivery = future.result()
                except SyncPermanentError:
                    summary["failed"] = int(summary["failed"]) + 1
                    interrupted = True
                except SyncTransientError:
                    summary["pending"] = int(summary["pending"]) + 1
                    interrupted = True
                except BaseException as exc:
                    # Preserve the existing propagation behavior for unexpected
                    # failures, while allowing already-claimed batches to finish
                    # and remain resumable before the exception is re-raised.
                    if unexpected is None:
                        unexpected = exc
                    interrupted = True
                else:
                    self._record_delivery(summary, delivery)

        try:
            pending_replays = self.outbox.pending_batches()
            with ThreadPoolExecutor(
                max_workers=options.batch_concurrency,
                thread_name_prefix="ustc-sync-batch",
            ) as executor:
                # Replay every persisted batch before claiming any new work.
                # Claims happen only in this coordinator thread; each worker
                # receives an immutable batch and opens its own DB sessions.
                while not interrupted and can_claim() and (
                    replay_index < len(pending_replays) or in_flight
                ):
                    while (
                        not interrupted
                        and can_claim()
                        and replay_index < len(pending_replays)
                        and len(in_flight) < options.batch_concurrency
                    ):
                        pending = pending_replays[replay_index]
                        replay_index += 1
                        batch = self.outbox.build_batch(
                            run_id=client_run_id,
                            batch_id=pending.id,
                            producer_version=options.producer_version,
                            observed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                            limit=options.batch_size,
                            max_payload_bytes=options.max_payload_bytes,
                        )
                        if batch is not None:
                            record_claim(batch, replayed=True)
                    if in_flight:
                        done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                        process_completed(done)

                # Only after all replay work has completed may new batches be
                # claimed.  This keeps replay-first ordering strict even when
                # the configured batch concurrency is greater than one.
                while not interrupted and can_claim() and not new_batches_exhausted:
                    while (
                        not interrupted
                        and can_claim()
                        and len(in_flight) < options.batch_concurrency
                    ):
                        try:
                            batch = self.outbox.build_batch(
                                run_id=client_run_id,
                                batch_id=uuid.uuid4().hex,
                                producer_version=options.producer_version,
                                observed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                                limit=options.batch_size,
                                max_payload_bytes=options.max_payload_bytes,
                            )
                        except BatchTooLargeError:
                            summary["failed"] = int(summary["failed"]) + 1
                            summary["pending"] = int(summary["pending"]) + 1
                            interrupted = True
                            break
                        if batch is None:
                            new_batches_exhausted = True
                            break
                        record_claim(batch, replayed=False)
                    if in_flight:
                        done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                        process_completed(done)
                    elif not interrupted:
                        break

                # A failure stops further claims but never abandons batches
                # already submitted to workers.  Drain the executor so every
                # in-flight batch records its durable result before the run is
                # finalized.
                while in_flight:
                    done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                    process_completed(done)
        except BaseException as exc:
            if unexpected is None:
                unexpected = exc
            interrupted = True
        finally:
            summary["status"] = "partial" if interrupted else "completed"
            self._finish_run(
                client_run_id,
                status=str(summary["status"]),
                batches=int(summary["batches"]),
                items=int(summary["items"]),
                errors=int(summary["failed"]),
            )
        if unexpected is not None:
            raise unexpected
        return summary

    @staticmethod
    def _record_delivery(summary: dict[str, int | str], delivery: DeliveryResult) -> None:
        summary["rejected"] = int(summary.get("rejected", 0)) + delivery.rejected
        if delivery.status == "acked":
            summary["acked"] = int(summary["acked"]) + 1
        else:
            summary["failed"] = int(summary["failed"]) + 1

    def _deliver(self, batch: IngestionBatch, options: SyncOptions) -> DeliveryResult:
        self.outbox.mark_batch(batch.batch_id, status="uploading")
        try:
            response = self._post_batch(batch, options)
            accepted_identities, rejected_identities = self._result_identities(batch, response)
            accepted_item_keys = self.outbox.batch_item_keys(
                batch.batch_id,
                accepted_identities,
            )
            self._upload_objects(batch.batch_id, item_keys=accepted_item_keys, options=options)
            status = self.outbox.mark_batch_results(
                batch.batch_id,
                accepted_identities=accepted_identities,
                rejected_identities=rejected_identities,
                response_json=self._safe_response_json(response),
            )
            return DeliveryResult(status, len(accepted_identities), len(rejected_identities))
        except SyncPermanentError as exc:
            self.outbox.mark_batch(batch.batch_id, status="failed")
            self.outbox.mark_batch_error(batch.batch_id, exc.code)
            raise
        except SyncTransientError as exc:
            self.outbox.mark_batch_error(batch.batch_id, exc.code)
            raise
    @staticmethod
    def _result_identities(
        batch: IngestionBatch,
        response: IngestionBatchResponse,
    ) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
        expected = Counter(
            (item.source_id, item.canonical_url, item.revision_hash) for item in batch.items
        )
        returned = Counter(
            (item.source_id, item.canonical_url, item.revision_hash)
            for item in response.results
        )
        if returned != expected:
            raise SyncProtocolError("batch_result_membership_mismatch")
        statuses: dict[tuple[str, str, str], set[str]] = {}
        for item in response.results:
            identity = (item.source_id, item.canonical_url, item.revision_hash)
            statuses.setdefault(identity, set()).add(item.status)
        if any(len(values) != 1 for values in statuses.values()):
            raise SyncProtocolError("batch_result_duplicate_status")
        rejected = {identity for identity, values in statuses.items() if "rejected" in values}
        accepted = set(statuses) - rejected
        return accepted, rejected

    @staticmethod
    def _safe_response_json(response: IngestionBatchResponse) -> str:
        """Persist response identity/status without arbitrary server errors."""

        value = {
            "batchId": response.batch_id,
            "clientRunId": response.client_run_id,
            "payloadDigest": response.payload_digest,
            "results": [
                {
                    "sourceId": item.source_id,
                    "canonicalUrl": item.canonical_url,
                    "revisionHash": item.revision_hash,
                    "status": item.status,
                    "publicationId": item.publication_id,
                    "revisionId": item.revision_id,
                }
                for item in response.results
            ],
        }
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _post_batch(self, batch: IngestionBatch, options: SyncOptions) -> IngestionBatchResponse:
        response = self._api_request(
            "POST",
            BATCH_ENDPOINT,
            options=options,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": batch.batch_id,
            },
            content=batch.payload_bytes(),
        )
        self._require_success(response)
        try:
            parsed = IngestionBatchResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise SyncProtocolError("invalid_batch_response") from exc
        if parsed.batch_id != batch.batch_id or parsed.client_run_id != batch.client_run_id:
            raise SyncProtocolError("batch_response_identity_mismatch")
        if parsed.payload_digest != batch.payload_sha256():
            raise SyncProtocolError("batch_response_digest_mismatch")
        return parsed

    def _upload_objects(
        self,
        batch_id: str,
        *,
        item_keys: set[str],
        options: SyncOptions,
    ) -> None:
        local_manifests = self.outbox.batch_objects(
            batch_id,
            item_keys=item_keys,
        )
        manifest_groups: dict[tuple[str, str], list[LocalObjectManifest]] = {}
        for manifest in local_manifests:
            key = (manifest.kind, manifest.sha256)
            group = manifest_groups.setdefault(key, [])
            if group:
                previous = group[0]
                # Link metadata and MIME aliases may differ between pages.
                # The digest and size identify the immutable bytes; the
                # server's first stored MIME remains authoritative.
                if previous.size != manifest.size:
                    raise SyncProtocolError("duplicate_object_manifest")
            group.append(manifest)
        manifests = {key: group[0] for key, group in manifest_groups.items()}
        if not manifests:
            return
        object_items = [
            PublicationObjectPlanRequestItem(kind=kind, sha256=sha256)
            for kind, sha256 in sorted(manifests)
        ]
        plan_window = min(MAX_OBJECT_PLAN_OBJECTS, options.object_concurrency)
        with ThreadPoolExecutor(
            max_workers=options.object_concurrency,
            thread_name_prefix="ustc-sync-object",
        ) as executor:
            # Check existing objects in protocol-sized chunks. Only missing
            # objects need signed URLs, and any URL that cannot start in the
            # first concurrency window is refreshed immediately before use.
            for start in range(0, len(object_items), MAX_OBJECT_PLAN_OBJECTS):
                chunk = object_items[start : start + MAX_OBJECT_PLAN_OBJECTS]
                initial_plan = self._plan_objects(batch_id, chunk, options)
                missing = sorted(
                    key
                    for key, item in initial_plan.items()
                    if item.status == "upload_required"
                )
                for window_start in range(0, len(missing), plan_window):
                    window_keys = missing[window_start : window_start + plan_window]
                    if window_start == 0:
                        planned = {key: initial_plan[key] for key in window_keys}
                    else:
                        planned = self._plan_objects(
                            batch_id,
                            [
                                PublicationObjectPlanRequestItem(
                                    kind=kind,
                                    sha256=sha256,
                                )
                                for kind, sha256 in window_keys
                            ],
                            options,
                        )

                    ordered = sorted(planned.items())
                    for key, item in ordered:
                        if item.status != "upload_required":
                            continue
                        if item.upload_url is None:
                            raise SyncProtocolError("object_upload_url_missing")
                        for manifest in manifest_groups[key]:
                            self._object_bytes(manifest)

                    pending: dict[tuple[str, str], Future[None]] = {
                        key: executor.submit(
                            self._upload_object,
                            batch_id,
                            item,
                            manifests[key],
                            options,
                        )
                        for key, item in ordered
                    }
                    for key, _item in ordered:
                        try:
                            pending[key].result()
                        except BaseException:
                            for remaining in pending.values():
                                remaining.cancel()
                            raise

    def _plan_objects(
        self,
        batch_id: str,
        object_items: list[PublicationObjectPlanRequestItem],
        options: SyncOptions,
    ) -> dict[tuple[str, str], PublicationObjectPlanItem]:
        request = PublicationObjectPlanRequest(batchId=batch_id, objects=object_items)
        plan_response = self._api_request(
            "POST",
            OBJECT_PLAN_ENDPOINT,
            options=options,
            headers={"Content-Type": "application/json"},
            content=_json_bytes(request.model_dump(by_alias=True, mode="json")),
        )
        self._require_success(plan_response)
        try:
            plan = PublicationObjectPlanResponse.model_validate(plan_response.json())
        except (ValueError, TypeError) as exc:
            raise SyncProtocolError("invalid_object_plan_response") from exc
        if plan.batch_id != batch_id:
            raise SyncProtocolError("object_plan_identity_mismatch")
        expected = {(item.kind, item.sha256) for item in object_items}
        planned: dict[tuple[str, str], PublicationObjectPlanItem] = {}
        for item in plan.objects:
            key = (item.kind, item.sha256)
            if key in planned or key not in expected:
                raise SyncProtocolError("object_plan_membership_mismatch")
            planned[key] = item
        if set(planned) != expected:
            raise SyncProtocolError("object_plan_membership_mismatch")
        return planned

    def _upload_object(
        self,
        batch_id: str,
        item: PublicationObjectPlanItem,
        manifest: LocalObjectManifest,
        options: SyncOptions,
    ) -> None:
        if item.status == "already_present":
            return
        if item.upload_url is None:
            raise SyncProtocolError("object_upload_url_missing")
        body = self._object_bytes(manifest)
        upload = self._request(
            "PUT",
            item.upload_url,
            options=options,
            headers=item.required_headers.model_dump(by_alias=True, mode="json"),
            content=body,
        )
        self._require_success(upload)
        complete_request = PublicationObjectCompleteRequest(
            batchId=batch_id,
            kind=item.kind,
            sha256=item.sha256,
        )
        complete = self._api_request(
            "POST",
            OBJECT_COMPLETE_ENDPOINT,
            options=options,
            headers={"Content-Type": "application/json"},
            content=_json_bytes(complete_request.model_dump(by_alias=True, mode="json")),
        )
        self._require_success(complete)
        try:
            complete_response = PublicationObjectCompleteResponse.model_validate(complete.json())
        except (ValueError, TypeError) as exc:
            raise SyncProtocolError("invalid_object_complete_response") from exc
        if (
            complete_response.batch_id != batch_id
            or complete_response.kind != item.kind
            or complete_response.sha256 != item.sha256
        ):
            raise SyncProtocolError("object_complete_identity_mismatch")

    @staticmethod
    def _object_bytes(manifest: LocalObjectManifest) -> bytes:
        try:
            body = Path(manifest.local_path).read_bytes()
        except OSError as exc:
            raise ImmutableObjectChangedError("immutable_object_changed") from exc
        if len(body) != manifest.size or hashlib.sha256(body).hexdigest() != manifest.sha256:
            raise ImmutableObjectChangedError("immutable_object_changed")
        return body

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        options: SyncOptions,
        headers: dict[str, str],
        **kwargs: Any,
    ) -> httpx.Response:
        request_headers = {
            **headers,
            INGESTION_SECRET_HEADER: self._ingestion_secret,
        }
        return self._request(
            method,
            f"{self.server}{path}",
            options=options,
            headers=request_headers,
            **kwargs,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        options: SyncOptions,
        headers: dict[str, str],
        **kwargs: Any,
    ) -> httpx.Response:
        for attempt in range(options.max_retries + 1):
            try:
                response = self.http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if attempt >= options.max_retries:
                    raise SyncTransientError("network_error") from exc
                self._sleep(min(options.max_backoff, 2**attempt))
                continue
            if response.status_code in RETRY_STATUS_CODES or response.status_code >= 500:
                if attempt >= options.max_retries:
                    raise SyncTransientError(f"retry_exhausted_http_{response.status_code}")
                delay = _retry_after(response, self._now)
                self._sleep(min(options.max_backoff, delay if delay is not None else 2**attempt))
                continue
            return response
        raise SyncTransientError("network_error")

    @staticmethod
    def _require_success(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if 400 <= response.status_code < 500:
            raise SyncPermanentError(_error_code(response))
        raise SyncTransientError(f"http_{response.status_code}")

    def _start_run(self, run_id: str) -> None:
        with self.database.session_factory.begin() as session:
            if session.get(SyncRun, run_id) is None:
                session.add(
                    SyncRun(
                        id=run_id,
                        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        mode="sync",
                        status="running",
                    )
                )

    def _finish_run(
        self,
        run_id: str,
        *,
        status: str,
        batches: int,
        items: int,
        errors: int,
    ) -> None:
        with self.database.session_factory.begin() as session:
            run = session.get(SyncRun, run_id)
            if run is None:
                return
            run.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            run.status = status
            run.pages = batches
            run.articles = items
            run.errors = errors


def sync_backfill(store: Store, *, chunk_size: int = 100) -> dict[str, int]:
    """Keyset-enqueue existing articles without making any network requests."""

    if chunk_size < 1 or chunk_size > 1_000:
        raise ValueError("backfill chunk size must be between 1 and 1000")
    outbox = IngestionOutbox(store.database)
    sources: dict[str, Any] = {}
    cursor = ""
    result = {"scanned": 0, "enqueued": 0, "errors": 0}
    while True:
        snapshots = store.sync_article_snapshot_page(cursor, chunk_size)
        if not snapshots:
            break
        for snapshot in snapshots:
            article = snapshot.article
            result["scanned"] += 1
            cursor = article.url
            try:
                source = sources.get(article.source_id)
                if source is None:
                    source = store.source_descriptor(article.source_id)
                    sources[article.source_id] = source
                if source.discovery_only:
                    continue
                _event_id, created = outbox.enqueue_article_with_status(
                    article,
                    store.data_dir,
                    source=source,
                    media_paths=snapshot.media_paths,
                    asset_paths=snapshot.asset_paths,
                )
            except (OSError, ValueError, KeyError):
                result["errors"] += 1
                continue
            if created:
                result["enqueued"] += 1
    return result


SyncClient = IngestionSyncClient


__all__ = [
    "BATCH_ENDPOINT",
    "DEFAULT_OBJECT_CONCURRENCY",
    "DEFAULT_BATCH_CONCURRENCY",
    "DEFAULT_HTTP_TIMEOUT",
    "MAX_BATCH_CONCURRENCY",
    "MAX_OBJECT_CONCURRENCY",
    "OBJECT_COMPLETE_ENDPOINT",
    "OBJECT_PLAN_ENDPOINT",
    "INGESTION_SECRET_ENV",
    "INGESTION_SECRET_HEADER",
    "ImmutableObjectChangedError",
    "IngestionSyncClient",
    "ingestion_secret_from_environment",
    "SyncClient",
    "SyncClientError",
    "SyncOptions",
    "SyncPermanentError",
    "SyncProtocolError",
    "SyncTransientError",
    "sync_backfill",
]
