"""HTTPX client for durable publication batch replay and object uploads."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

from .. import __version__
from ..db.models import SyncBatch, SyncRun
from .models import (
    MAX_OBJECT_PLAN_OBJECTS,
    MAX_PUBLICATION_BATCH_ITEMS,
    IngestionBatch,
    IngestionBatchResponse,
    LocalObjectManifest,
    PublicationObjectPlanItem,
    PublicationObjectPlanRequest,
    PublicationObjectPlanRequestItem,
    PublicationObjectPlanResponse,
    PublicationObjectUploadResponse,
)
from .outbox import (
    DEFAULT_MAX_BATCH_BYTES,
    IngestionOutbox,
)

if TYPE_CHECKING:
    from ..store import Store

BATCH_ENDPOINT = "/api/ingestion/publications/batches"
OBJECT_PLAN_ENDPOINT = "/api/ingestion/publications/objects/plan"
OBJECT_UPLOAD_PREFIX = "/api/ingestion/publications/objects"
INGESTION_SECRET_ENV = "USTC_CRAWLER_INGESTION_SECRET"
INGESTION_SECRET_HEADER = "X-Publication-Ingestion-Secret"
RETRY_STATUS_CODES = frozenset({408, 429})
RETRYABLE_HTTP_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
)
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
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("server must be an HTTP(S) origin")
    return f"{parsed.scheme}://{parsed.netloc}"


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
    except ValueError:
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
                # HTTP-dates are always GMT; a naive parse must be read as
                # UTC, not as local time.
                target = target.replace(tzinfo=UTC)
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
        self._archive_object_cache: dict[str, dict[str, bytes]] = {}
        self._archive_object_lock = threading.Lock()

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
            raise ValueError(f"batch size must be between 1 and {MAX_PUBLICATION_BATCH_ITEMS}")
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
            raise ValueError(f"object concurrency must be between 1 and {MAX_OBJECT_CONCURRENCY}")
        if not 1 <= options.batch_concurrency <= MAX_BATCH_CONCURRENCY:
            raise ValueError(f"batch concurrency must be between 1 and {MAX_BATCH_CONCURRENCY}")
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
        in_flight: dict[Future[DeliveryResult], tuple[int, str]] = {}
        sequence = 0

        def can_claim() -> bool:
            return not options.max_batches or int(summary["batches"]) < options.max_batches

        def log_progress(batch_id: str, outcome: str) -> None:
            print(
                f"sync batch {batch_id}: {outcome}"
                f" acked={summary['acked']} failed={summary['failed']}"
                f" pending={summary['pending']}",
                file=sys.stderr,
                flush=True,
            )

        previous_sigterm: Any = None
        if threading.current_thread() is threading.main_thread():
            # SIGTERM (e.g. `timeout --signal=TERM`) flips the same interrupted
            # flag a transient failure uses, so the run drains in-flight
            # batches and records its summary instead of dying mid-flight.
            def _sigterm_interrupt(_signum: int, _frame: Any) -> None:
                nonlocal interrupted
                interrupted = True

            previous_sigterm = signal.signal(signal.SIGTERM, _sigterm_interrupt)

        def record_claim(batch: IngestionBatch, *, replayed: bool) -> None:
            nonlocal sequence
            summary["batches"] = int(summary["batches"]) + 1
            summary["items"] = int(summary["items"]) + len(batch.items)
            if replayed:
                summary["replayed"] = int(summary["replayed"]) + 1
            else:
                summary["created"] = int(summary["created"]) + 1
            future = executor.submit(self._deliver, batch, options)
            in_flight[future] = (sequence, batch.batch_id)
            sequence += 1

        def process_completed(done: set[Future[DeliveryResult]]) -> None:
            nonlocal interrupted, unexpected
            for future in sorted(done, key=lambda item: in_flight[item][0]):
                _sequence, batch_id = in_flight.pop(future)
                try:
                    delivery = future.result()
                except SyncPermanentError as exc:
                    # A permanently rejected batch is terminal for that batch
                    # only; it must not poison the rest of the run, or a single
                    # undeliverable batch would block the queue forever.
                    summary["failed"] = int(summary["failed"]) + 1
                    log_progress(batch_id, f"failed:{exc.code}")
                except SyncTransientError as exc:
                    summary["pending"] = int(summary["pending"]) + 1
                    interrupted = True
                    log_progress(batch_id, f"pending:{exc.code}")
                except BaseException as exc:
                    # Preserve the existing propagation behavior for unexpected
                    # failures, while allowing already-claimed batches to finish
                    # and remain resumable before the exception is re-raised.
                    if unexpected is None:
                        unexpected = exc
                    interrupted = True
                    log_progress(batch_id, f"error:{type(exc).__name__}")
                else:
                    self._record_delivery(summary, delivery)
                    log_progress(batch_id, delivery.status)

        try:
            pending_replays = self.outbox.pending_batches()
            with ThreadPoolExecutor(
                max_workers=options.batch_concurrency,
                thread_name_prefix="ustc-sync-batch",
            ) as executor:
                # Replay every persisted batch before claiming any new work.
                # Claims happen only in this coordinator thread; each worker
                # receives an immutable batch and opens its own DB sessions.
                while (
                    not interrupted
                    and can_claim()
                    and (replay_index < len(pending_replays) or in_flight)
                ):
                    while (
                        not interrupted
                        and can_claim()
                        and replay_index < len(pending_replays)
                        and len(in_flight) < options.batch_concurrency
                    ):
                        pending = pending_replays[replay_index]
                        replay_index += 1
                        try:
                            batch = self.outbox.build_batch(
                                run_id=client_run_id,
                                batch_id=pending.id,
                                producer_version=options.producer_version,
                                observed_at=datetime.now()
                                .astimezone()
                                .isoformat(timespec="seconds"),
                                limit=options.batch_size,
                                max_payload_bytes=options.max_payload_bytes,
                            )
                        except Exception:
                            # One unrebuildable persisted batch (e.g. a legacy
                            # batch above the protocol item limit) must not
                            # abort the run and wedge every later replay.
                            self.outbox.mark_batch(pending.id, status="failed")
                            self.outbox.mark_batch_error(pending.id, "batch_rebuild_error")
                            summary["failed"] = int(summary["failed"]) + 1
                            log_progress(pending.id, "failed:batch_rebuild_error")
                            continue
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
                        batch = self.outbox.build_batch(
                            run_id=client_run_id,
                            batch_id=uuid.uuid4().hex,
                            producer_version=options.producer_version,
                            observed_at=datetime.now()
                            .astimezone()
                            .isoformat(timespec="seconds"),
                            limit=options.batch_size,
                            max_payload_bytes=options.max_payload_bytes,
                        )
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
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
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
            accepted_identities, rejected_identities, unchanged_identities = (
                self._result_identities(batch, response)
            )
            accepted_item_keys = self.outbox.batch_item_keys(
                batch.batch_id,
                accepted_identities,
            )
            # The server registers batch object claims only when it creates or
            # updates a revision; "unchanged" items have no claims and their
            # bytes were delivered with the batch that first created them.
            upload_item_keys = accepted_item_keys - self.outbox.batch_item_keys(
                batch.batch_id,
                unchanged_identities,
            )
            self._upload_objects(batch.batch_id, item_keys=upload_item_keys, options=options)
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
    ) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]], set[tuple[str, str, str]]]:
        expected = Counter(
            (item.source_id, item.canonical_url, item.revision_hash) for item in batch.items
        )
        returned = Counter(
            (item.source_id, item.canonical_url, item.revision_hash) for item in response.results
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
        unchanged = {identity for identity, values in statuses.items() if "unchanged" in values}
        return accepted, rejected, unchanged

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
                    key for key, item in initial_plan.items() if item.status == "upload_required"
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
                    bodies: dict[tuple[str, str], bytes] = {}
                    for key, item in ordered:
                        if item.status != "upload_required":
                            continue
                        if item.upload_url is None:
                            raise SyncProtocolError("object_upload_url_missing")
                        for manifest in manifest_groups[key]:
                            body = self._object_bytes(manifest)
                            bodies.setdefault(key, body)

                    pending: dict[tuple[str, str], Future[None]] = {
                        key: executor.submit(
                            self._upload_object,
                            batch_id,
                            item,
                            manifests[key],
                            options,
                            bodies.get(key),
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
        body: bytes | None = None,
    ) -> None:
        if item.status == "already_present":
            return
        if item.upload_url is None:
            raise SyncProtocolError("object_upload_url_missing")
        upload_path = "/".join(
            [
                OBJECT_UPLOAD_PREFIX,
                quote(batch_id, safe="-_.!~*'()"),
                quote(item.kind, safe="-_.!~*'()"),
                quote(item.sha256, safe="-_.!~*'()"),
            ]
        )
        expected_upload_url = f"{self.server}{upload_path}"
        if item.upload_url != expected_upload_url:
            raise SyncProtocolError("object_upload_url_mismatch")
        if body is None:
            body = self._object_bytes(manifest)
        upload = self._api_request(
            "PUT",
            upload_path,
            options=options,
            headers=item.required_headers.model_dump(by_alias=True, mode="json"),
            content=body,
        )
        self._require_success(upload)
        try:
            upload_response = PublicationObjectUploadResponse.model_validate(upload.json())
        except (ValueError, TypeError) as exc:
            raise SyncProtocolError("invalid_object_upload_response") from exc
        if (
            upload_response.batch_id != batch_id
            or upload_response.kind != item.kind
            or upload_response.sha256 != item.sha256
        ):
            raise SyncProtocolError("object_upload_identity_mismatch")

    def _object_path(self, local_path: str) -> Path:
        path = Path(local_path)
        if path.is_absolute():
            return path
        parts = path.parts
        # Legacy manifests store cwd-relative paths that include the data dir
        # prefix (e.g. "data/sync-objects/..."); strip it and anchor at this
        # client's data dir so sync works from any working directory.
        if parts and parts[0] == self.data_dir.name:
            path = Path(*parts[1:])
        return self.data_dir / path

    def _object_bytes(self, manifest: LocalObjectManifest) -> bytes:
        try:
            body = self._object_path(manifest.local_path).read_bytes()
        except OSError:
            body = self._archived_object_bytes(manifest)
        if len(body) != manifest.size or hashlib.sha256(body).hexdigest() != manifest.sha256:
            raise ImmutableObjectChangedError("immutable_object_changed")
        return body

    def _archived_object_bytes(self, manifest: LocalObjectManifest) -> bytes:
        """Rebuild body object bytes from the articles table.

        The spool file is missing when the event was enqueued on another
        machine (the shared crawl state ships only the database, not the
        object spool).  Article ``body_html``/``body_markdown`` columns hold
        the same sanitized bytes the manifest was hashed from.  Media and
        asset objects have no archived copy and still fail permanently.

        Rows are queried on demand and pre-filtered by stored byte length
        (sanitization only ever removes characters), and only objects that
        actually matched a manifest digest are cached, so a large archive
        is never materialized as a whole-table sha->bytes snapshot.
        """
        if manifest.kind not in ("body_html", "body_markdown"):
            raise ImmutableObjectChangedError("immutable_object_changed")
        with self._archive_object_lock:
            body = self._archive_object_cache.get(manifest.kind, {}).get(manifest.sha256)
        if body is not None:
            return body
        from sqlalchemy import text

        from ..models import sanitize_text

        column = manifest.kind
        with self.database.session_factory() as session:
            rows = session.execute(
                text(
                    f"SELECT {column} FROM articles"
                    f" WHERE {column} IS NOT NULL AND {column} != ''"
                    f" AND length(CAST({column} AS BLOB)) >= :size"
                ),
                {"size": manifest.size},
            )
            for (value,) in rows:
                candidate = sanitize_text(str(value)).encode("utf-8")
                if len(candidate) != manifest.size:
                    continue
                if hashlib.sha256(candidate).hexdigest() != manifest.sha256:
                    continue
                with self._archive_object_lock:
                    self._archive_object_cache.setdefault(manifest.kind, {})[
                        manifest.sha256
                    ] = candidate
                return candidate
        raise ImmutableObjectChangedError("immutable_object_changed")

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
            except RETRYABLE_HTTP_ERRORS as exc:
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

    @staticmethod
    def _require_success(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if 300 <= response.status_code < 400:
            # follow_redirects=False: a redirect means the endpoint moved
            # (or an auth gateway intercepted). Spinning on it retried the
            # same batch forever, so fail it terminally with a safe code.
            raise SyncPermanentError("http_redirect")
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
    "OBJECT_UPLOAD_PREFIX",
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
