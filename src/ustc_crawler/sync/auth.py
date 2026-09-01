"""OAuth 2.0 device authorization for the pre-registered crawler client."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

OAUTH_SCOPE = "publication.ingest:write offline_access"
RESOURCE_PATH = "/api/auth"
PREFERRED_DISCOVERY_PATH = "/.well-known/oauth-authorization-server/api/auth"
FALLBACK_DISCOVERY_PATH = "/.well-known/oauth-authorization-server"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
OAUTH_ERROR_CODES = frozenset(
    {
        "access_denied",
        "authorization_pending",
        "expired_token",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_target",
        "invalid_scope",
        "slow_down",
        "server_error",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_grant_type",
    }
)


class CredentialStore(Protocol):
    """Minimal storage interface; implementations must not use SQLite."""

    def load(self) -> OAuthTokenState | None: ...

    def save(self, state: OAuthTokenState) -> None: ...

    def delete(self) -> None: ...


class OAuthClientError(RuntimeError):
    """Base class for actionable OAuth client failures."""


class CredentialStoreUnavailable(OAuthClientError):
    """Raised when no secure OS credential backend is available."""


class OAuthProtocolError(OAuthClientError):
    """A protocol/network failure without response-body or token disclosure."""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"OAuth request failed: {code}{suffix}")


class OAuthTokenState(BaseModel):
    """Only the credential material stored in the OS keyring."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    access_token: str = Field(min_length=1)
    refresh_token: str | None = Field(default=None, min_length=1)
    token_type: str = Field(default="Bearer", min_length=1)
    expires_at: float = Field(gt=0)
    scope: str | None = None


class OAuthMetadata(BaseModel):
    """Required subset of RFC 8414 authorization-server metadata."""

    model_config = ConfigDict(extra="ignore", strict=True)

    issuer: str | None = None
    device_authorization_endpoint: str
    token_endpoint: str

    @field_validator("device_authorization_endpoint", "token_endpoint")
    @classmethod
    def _absolute_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OAuth endpoint must be an absolute HTTP(S) URL")
        return value


class DeviceAuthorizationResponse(BaseModel):
    """RFC 8628 device authorization response."""

    model_config = ConfigDict(extra="ignore", strict=True)

    device_code: str = Field(alias="device_code", min_length=1)
    user_code: str = Field(alias="user_code", min_length=1)
    verification_uri: str = Field(alias="verification_uri", min_length=1)
    verification_uri_complete: str | None = Field(
        default=None,
        alias="verification_uri_complete",
    )
    expires_in: int = Field(alias="expires_in", gt=0)
    interval: int = Field(default=5, ge=1)

    @field_validator("verification_uri", "verification_uri_complete")
    @classmethod
    def _absolute_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("verification URI must be an absolute HTTP(S) URL")
        return value


class OAuthTokenResponse(BaseModel):
    """Token endpoint response; unknown provider metadata is ignored."""

    model_config = ConfigDict(extra="ignore", strict=True)

    access_token: str = Field(alias="access_token", min_length=1)
    token_type: str = Field(default="Bearer", alias="token_type", min_length=1)
    expires_in: int = Field(alias="expires_in", gt=0)
    refresh_token: str | None = Field(default=None, alias="refresh_token", min_length=1)
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceInstructions:
    """Safe-to-display device login instructions (never includes device_code)."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


class KeyringCredentialStore:
    """Persist tokens through a configured secure OS keyring only."""

    SERVICE = "life-ustc/crawler-oauth"

    def __init__(self, server: str, client_id: str) -> None:
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - dependency is locked
            raise CredentialStoreUnavailable(
                "Install the keyring package and configure an OS credential store"
            ) from exc
        self._keyring = keyring
        normalized_server = server.strip().rstrip("/")
        normalized_client = client_id.strip()
        self._username = hashlib.sha256(
            f"{normalized_server}\n{normalized_client}".encode()
        ).hexdigest()
        self._require_secure_backend()

    def _require_secure_backend(self) -> None:
        try:
            backend = self._keyring.get_keyring()
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "Secure OS keyring could not be initialized; configure Keychain, "
                "Secret Service, or Windows Credential Manager"
            ) from exc
        priority = getattr(backend, "priority", 0) or 0
        module = type(backend).__module__.lower()
        name = type(backend).__name__.lower()
        insecure_markers = (
            "keyrings.alt",
            "plaintext",
            "basicfile",
            "filekeyring",
        )
        if priority <= 0 or module.startswith("keyring.backends.fail") or any(
            marker in module or marker in name for marker in insecure_markers
        ):
            raise CredentialStoreUnavailable(
                "No secure OS keyring is available; configure Keychain, Secret Service, "
                "or Windows Credential Manager instead of using plaintext storage"
            )

    def load(self) -> OAuthTokenState | None:
        try:
            raw = self._keyring.get_password(self.SERVICE, self._username)
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "Secure OS keyring could not read crawler credentials"
            ) from exc
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            return OAuthTokenState.model_validate(value)
        except (ValueError, TypeError) as exc:
            raise CredentialStoreUnavailable(
                "Stored crawler credentials are invalid; run auth logout then auth login"
            ) from exc

    def save(self, state: OAuthTokenState) -> None:
        payload = json.dumps(state.model_dump(mode="json"), separators=(",", ":"))
        try:
            self._keyring.set_password(self.SERVICE, self._username, payload)
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "Secure OS keyring could not save crawler credentials"
            ) from exc

    def delete(self) -> None:
        try:
            if self._keyring.get_password(self.SERVICE, self._username) is None:
                return
            self._keyring.delete_password(self.SERVICE, self._username)
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "Secure OS keyring could not remove crawler credentials"
            ) from exc


def _server_base(server: str) -> str:
    value = server.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--server must be an absolute HTTP(S) URL")
    return value


def _json_body(response: httpx.Response) -> Mapping[str, Any] | None:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _oauth_error(response: httpx.Response) -> OAuthProtocolError:
    body = _json_body(response) or {}
    value = body.get("error")
    code = value if isinstance(value, str) and value in OAUTH_ERROR_CODES else "http_error"
    return OAuthProtocolError(code, response.status_code)


class OAuthDeviceClient:
    """RFC 8628 device and refresh-token client for the ingestion API."""

    def __init__(
        self,
        server: str,
        client_id: str,
        credentials: CredentialStore,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not client_id.strip():
            raise ValueError("--client-id must not be empty")
        self.server = _server_base(server)
        self.client_id = client_id.strip()
        self.resource = f"{self.server}{RESOURCE_PATH}"
        self.credentials = credentials
        self.http = http_client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._owns_http = http_client is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def discover(self) -> OAuthMetadata:
        preferred = f"{self.server}{PREFERRED_DISCOVERY_PATH}"
        response = self._request("GET", preferred)
        if response.status_code == 404:
            response = self._request("GET", f"{self.server}{FALLBACK_DISCOVERY_PATH}")
        if not response.is_success:
            raise _oauth_error(response)
        try:
            return OAuthMetadata.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise OAuthProtocolError("invalid_discovery_metadata") from exc

    def request_device_authorization(
        self,
        *,
        metadata: OAuthMetadata | None = None,
    ) -> DeviceAuthorizationResponse:
        metadata = metadata or self.discover()
        response = self._request(
            "POST",
            metadata.device_authorization_endpoint,
            data={
                "client_id": self.client_id,
                "scope": OAUTH_SCOPE,
                "resource": self.resource,
            },
        )
        if not response.is_success:
            raise _oauth_error(response)
        try:
            return DeviceAuthorizationResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise OAuthProtocolError("invalid_device_authorization_response") from exc

    def login(
        self,
        *,
        on_instructions: Callable[[DeviceInstructions], None] | None = None,
    ) -> OAuthTokenState:
        metadata = self.discover()
        device = self.request_device_authorization(metadata=metadata)
        instructions = DeviceInstructions(
            user_code=device.user_code,
            verification_uri=device.verification_uri,
            verification_uri_complete=device.verification_uri_complete,
            expires_in=device.expires_in,
            interval=device.interval,
        )
        if on_instructions is not None:
            on_instructions(instructions)
        deadline = self._monotonic() + device.expires_in
        interval = device.interval
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise OAuthProtocolError("expired_token")
            self._sleep(min(interval, remaining))
            if self._monotonic() >= deadline:
                raise OAuthProtocolError("expired_token")
            response = self._request(
                "POST",
                metadata.token_endpoint,
                data={
                    "grant_type": DEVICE_GRANT_TYPE,
                    "device_code": device.device_code,
                    "client_id": self.client_id,
                    "resource": self.resource,
                },
            )
            if response.is_success:
                state = self._token_state(response, None)
                self.credentials.save(state)
                return state
            error = _oauth_error(response)
            if error.code == "authorization_pending":
                continue
            if error.code == "slow_down":
                interval += 5
                continue
            raise error

    def refresh(self) -> OAuthTokenState:
        current = self.credentials.load()
        if current is None or not current.refresh_token:
            raise OAuthClientError("No refresh token is available; run auth login")
        metadata = self.discover()
        response = self._request(
            "POST",
            metadata.token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
                "client_id": self.client_id,
                "resource": self.resource,
            },
        )
        if not response.is_success:
            error = _oauth_error(response)
            if error.code == "invalid_grant":
                self.credentials.delete()
            raise error
        state = self._token_state(response, current)
        self.credentials.save(state)
        return state

    def access_token(self, *, force_refresh: bool = False) -> str:
        state = self.credentials.load()
        if state is None:
            raise OAuthClientError("No crawler credentials found; run auth login")
        if force_refresh or state.expires_at <= self._now() + 30:
            state = self.refresh()
        return state.access_token

    def status(self) -> dict[str, Any]:
        state = self.credentials.load()
        if state is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "expiresAt": state.expires_at,
            "expired": state.expires_at <= self._now(),
            "scope": state.scope,
            "hasRefreshToken": bool(state.refresh_token),
        }

    def logout(self) -> None:
        self.credentials.delete()

    def _token_state(
        self,
        response: httpx.Response,
        previous: OAuthTokenState | None,
    ) -> OAuthTokenState:
        try:
            token = OAuthTokenResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise OAuthProtocolError("invalid_token_response") from exc
        return OAuthTokenState(
            access_token=token.access_token,
            refresh_token=token.refresh_token or (previous.refresh_token if previous else None),
            token_type=token.token_type,
            expires_at=self._now() + token.expires_in,
            scope=token.scope or (previous.scope if previous else None),
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise OAuthProtocolError("network_error") from exc


__all__ = [
    "CredentialStore",
    "CredentialStoreUnavailable",
    "DeviceAuthorizationResponse",
    "DeviceInstructions",
    "FALLBACK_DISCOVERY_PATH",
    "KeyringCredentialStore",
    "OAuthClientError",
    "OAuthDeviceClient",
    "OAuthMetadata",
    "OAuthProtocolError",
    "OAuthTokenState",
    "OAUTH_SCOPE",
    "PREFERRED_DISCOVERY_PATH",
    "RESOURCE_PATH",
]
