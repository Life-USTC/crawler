"""HTTPX client for durable publication batch replay and object uploads."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .. import __version__
from ..db.models import SyncRun
from .auth import OAuthClientError, OAuthDeviceClient
from .models import (
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
RETRY_STATUS_CODES = frozenset({408, 429})
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


@dataclass(frozen=True, slots=True)
class SyncOptions:
    batch_size: int = 50
    max_payload_bytes: int = DEFAULT_MAX_BATCH_BYTES
    producer_version: str = f"ustc-public-site-crawler/{__version__}"
    max_batches: int = 0
    max_retries: int = 3
    max_backoff: float = 30.0


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
    """Replay immutable outbox batches and upload their referenced objects."""

    def __init__(
        self,
        database: Any,
        data_dir: str | Path,
        oauth: OAuthDeviceClient,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.data_dir = Path(data_dir)
        self.oauth = oauth
        self.http = http_client or oauth.http
        self._sleep = sleep
        self._now = now
        self.outbox = IngestionOutbox(database)

    def sync(
        self,
        *,
        options: SyncOptions | None = None,
        run_id: str | None = None,
    ) -> dict[str, int | str]:
        """Deliver pending batches, replaying persisted batches before claiming new work."""

        options = options or SyncOptions()
        if options.batch_size < 1 or options.batch_size > 500:
            raise ValueError("batch size must be between 1 and 500")
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
        try:
            for pending in self.outbox.pending_batches():
                if options.max_batches and int(summary["batches"]) >= options.max_batches:
                    break
                batch = self.outbox.build_batch(
                    run_id=client_run_id,
                    batch_id=pending.id,
                    producer_version=options.producer_version,
                    observed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    limit=options.batch_size,
                    max_payload_bytes=options.max_payload_bytes,
                )
                if batch is None:
                    continue
                summary["batches"] = int(summary["batches"]) + 1
                summary["replayed"] = int(summary["replayed"]) + 1
                summary["items"] = int(summary["items"]) + len(batch.items)
                try:
                    delivery = self._deliver(batch, options)
                except SyncPermanentError:
                    summary["failed"] = int(summary["failed"]) + 1
                    interrupted = True
                    break
                except SyncTransientError:
                    summary["pending"] = int(summary["pending"]) + 1
                    interrupted = True
                    break
                self._record_delivery(summary, delivery)

            while not interrupted and (
                not options.max_batches or int(summary["batches"]) < options.max_batches
            ):
                batch_id = uuid.uuid4().hex
                try:
                    batch = self.outbox.build_batch(
                        run_id=client_run_id,
                        batch_id=batch_id,
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
                    break
                summary["batches"] = int(summary["batches"]) + 1
                summary["created"] = int(summary["created"]) + 1
                summary["items"] = int(summary["items"]) + len(batch.items)
                try:
                    delivery = self._deliver(batch, options)
                except SyncPermanentError:
                    summary["failed"] = int(summary["failed"]) + 1
                    interrupted = True
                    break
                except SyncTransientError:
                    summary["pending"] = int(summary["pending"]) + 1
                    interrupted = True
                    break
                self._record_delivery(summary, delivery)
        finally:
            summary["status"] = "partial" if interrupted else "completed"
            self._finish_run(
                client_run_id,
                status=str(summary["status"]),
                batches=int(summary["batches"]),
                items=int(summary["items"]),
                errors=int(summary["failed"]),
            )
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
        except OAuthClientError as exc:
            code = getattr(exc, "code", "oauth_error")
            safe_code = f"oauth_{code}" if code else "oauth_error"
            self.outbox.mark_batch_error(batch.batch_id, safe_code)
            raise SyncTransientError(safe_code) from exc

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
        manifests: dict[tuple[str, str], LocalObjectManifest] = {}
        for manifest in local_manifests:
            key = (manifest.kind, manifest.sha256)
            previous = manifests.get(key)
            if previous is not None and previous != manifest:
                raise SyncProtocolError("duplicate_object_manifest")
            manifests[key] = manifest
        if not manifests:
            return
        request = PublicationObjectPlanRequest(
            batchId=batch_id,
            objects=[
                PublicationObjectPlanRequestItem(kind=kind, sha256=sha256)
                for kind, sha256 in sorted(manifests)
            ],
        )
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
        planned: dict[tuple[str, str], PublicationObjectPlanItem] = {}
        for item in plan.objects:
            key = (item.kind, item.sha256)
            if key in planned or key not in manifests:
                raise SyncProtocolError("object_plan_membership_mismatch")
            planned[key] = item
        if set(planned) != set(manifests):
            raise SyncProtocolError("object_plan_membership_mismatch")
        for key in sorted(planned):
            item = planned[key]
            if item.status == "upload_required":
                if item.upload_url is None:
                    raise SyncProtocolError("object_upload_url_missing")
                body = self._object_bytes(manifests[key])
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
                content=_json_bytes(
                    complete_request.model_dump(by_alias=True, mode="json")
                ),
            )
            self._require_success(complete)
            try:
                complete_response = PublicationObjectCompleteResponse.model_validate(
                    complete.json()
                )
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
        force_refresh = False
        for _ in range(2):
            token = self.oauth.access_token(force_refresh=force_refresh)
            request_headers = {"Authorization": f"Bearer {token}", **headers}
            response = self._request(
                method,
                f"{self.oauth.server}{path}",
                options=options,
                headers=request_headers,
                **kwargs,
            )
            if response.status_code != 401 or force_refresh:
                return response
            force_refresh = True
        raise SyncPermanentError("http_401")

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
        articles = store.sync_article_page(cursor, chunk_size)
        if not articles:
            break
        for article in articles:
            result["scanned"] += 1
            cursor = article.url
            try:
                source = sources.get(article.source_id)
                if source is None:
                    source = store.source_descriptor(article.source_id)
                    sources[article.source_id] = source
                _event_id, created = outbox.enqueue_article_with_status(
                    article,
                    store.data_dir,
                    source=source,
                    media_paths=store.media_paths_for_article(article.url),
                    asset_paths=store.asset_paths_for_article(
                        article.url,
                        article.source_page_url,
                    ),
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
    "OBJECT_COMPLETE_ENDPOINT",
    "OBJECT_PLAN_ENDPOINT",
    "ImmutableObjectChangedError",
    "IngestionSyncClient",
    "SyncClient",
    "SyncClientError",
    "SyncOptions",
    "SyncPermanentError",
    "SyncProtocolError",
    "SyncTransientError",
    "sync_backfill",
]
