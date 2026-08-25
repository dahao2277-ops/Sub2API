from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlsplit

APIYI_ORIGIN = "https://api.apiyi.com"
APIYI_HOST = "api.apiyi.com"
APIYI_PORT = 443
ALLOWED_PATHS = frozenset(("/v1/models", "/v1/chat/completions", "/v1/responses"))
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
SECRET_PATTERN = re.compile(
    r"(?i)(?:Bearer\s+)?(?:sk-|token-|key-)[A-Za-z0-9._~+/-]{8,}"
)
SENSITIVE_FIELDS = frozenset(
    ("authorization", "api_key", "apikey", "access_token", "key", "secret", "token")
)


class TransportPolicyError(RuntimeError):
    """The request could not be made without weakening the fixed-origin policy."""


@dataclass(frozen=True, slots=True)
class SanitizedHTTPError:
    status: int
    content_type: str
    request_id: str
    error_code: str
    error_message: str


class SecureResponse:
    def __init__(
        self,
        connection: http.client.HTTPSConnection,
        response: http.client.HTTPResponse,
        peer_ip: str,
    ) -> None:
        self._connection = connection
        self._response = response
        self.peer_ip = peer_ip
        self.status = int(response.status)
        self.headers = response.headers

    def read(self, amount: int = MAX_RESPONSE_BYTES + 1) -> bytes:
        return self._response.read(amount)

    def readline(self, amount: int) -> bytes:
        return self._response.readline(amount)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, timeout: float) -> None:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, APIYI_PORT, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._tls_context = context

    def connect(self) -> None:
        if getattr(self, "_tunnel_host", None):
            raise TransportPolicyError("proxy tunnels are forbidden")
        raw = socket.create_connection((self._pinned_ip, APIYI_PORT), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _default_resolver(host: str) -> tuple[str, ...]:
    addresses = {
        str(item[4][0])
        for item in socket.getaddrinfo(
            host,
            APIYI_PORT,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    }
    return tuple(sorted(addresses))


def _validated_addresses(addresses: tuple[str, ...]) -> tuple[str, ...]:
    if not addresses:
        raise TransportPolicyError("APIYI DNS returned no addresses")
    validated: list[str] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise TransportPolicyError(
                "APIYI DNS returned an invalid address"
            ) from error
        if not address.is_global:
            raise TransportPolicyError("APIYI DNS resolved outside the public Internet")
        validated.append(str(address))
    return tuple(sorted(set(validated)))


def validate_target(base_url: str, path: str) -> None:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != APIYI_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise TransportPolicyError("APIYI origin is not allowlisted")
    if path not in ALLOWED_PATHS or urlsplit(path).query or urlsplit(path).fragment:
        raise TransportPolicyError("APIYI path is not allowlisted")
    lowered = path.lower()
    if "bearer" in lowered or "sk-" in lowered or "secret" in lowered:
        raise TransportPolicyError("URL_CONTAINS_SECRET")


class APIYITransport:
    """Direct, proxy-free, DNS-pinned HTTPS transport for APIYI only."""

    def __init__(
        self,
        base_url: str = APIYI_ORIGIN,
        timeout_seconds: float = 20.0,
        *,
        resolver: Callable[[str], tuple[str, ...]] = _default_resolver,
        connection_factory: Callable[[str, str, float], http.client.HTTPSConnection]
        | None = None,
    ) -> None:
        validate_target(base_url, "/v1/models")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._resolver = resolver
        self._connection_factory = connection_factory or (
            lambda host, address, timeout: _PinnedHTTPSConnection(
                host, address, timeout
            )
        )

    def open(
        self,
        method: str,
        path: str,
        secret: str,
        *,
        body: bytes | None = None,
        accept: str = "application/json",
    ) -> SecureResponse:
        validate_target(self.base_url, path)
        if (
            method not in ("GET", "POST")
            or not secret
            or "\r" in secret
            or "\n" in secret
        ):
            raise TransportPolicyError("APIYI request is invalid")
        if method == "GET" and body is not None:
            raise TransportPolicyError("GET request body is forbidden")
        if method == "POST" and body is None:
            raise TransportPolicyError("POST request body is required")
        addresses = _validated_addresses(self._resolver(APIYI_HOST))
        pinned_ip = addresses[0]
        connection = self._connection_factory(
            APIYI_HOST, pinned_ip, self.timeout_seconds
        )
        authorization = "Bearer " + secret
        try:
            connection.connect()
            if connection.sock is None:
                raise TransportPolicyError("APIYI TLS connection is unavailable")
            peer_ip = str(connection.sock.getpeername()[0])
            if peer_ip != pinned_ip:
                raise TransportPolicyError("APIYI connection target changed")
            headers = {
                "Authorization": authorization,
                "Accept": accept,
                "User-Agent": "AI16T-Platform-B-Server/2",
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if 300 <= int(response.status) < 400:
                response.read(MAX_ERROR_BYTES)
                response.close()
                raise TransportPolicyError("APIYI redirect rejected")
            return SecureResponse(connection, response, peer_ip)
        except BaseException:
            connection.close()
            raise
        finally:
            authorization = ""


def sanitize_error(response: SecureResponse, secret: str) -> SanitizedHTTPError:
    payload = response.read(MAX_ERROR_BYTES + 1)
    if len(payload) > MAX_ERROR_BYTES:
        payload = b""
    content_type = (
        str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip()
    )
    request_id = ""
    for name in ("x-request-id", "x-api-request-id", "request-id"):
        value = response.headers.get(name)
        if value:
            request_id = _sanitize_text(str(value), secret)[:256]
            break
    code = ""
    message = ""
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        decoded = None
    sanitized = _sanitize_value(decoded, secret)
    source: Any = sanitized
    if isinstance(sanitized, dict) and isinstance(sanitized.get("error"), dict):
        source = sanitized["error"]
    if isinstance(source, dict):
        code = _sanitize_text(str(source.get("code", "")), secret)[:256]
        message = _sanitize_text(str(source.get("message", "")), secret)[:1024]
    elif isinstance(sanitized, str):
        message = sanitized[:1024]
    return SanitizedHTTPError(
        status=response.status,
        content_type=content_type,
        request_id=request_id,
        error_code=code,
        error_message=message,
    )


def _sanitize_value(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_value(item, secret)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize_value(item, secret) for item in value[:100]]
    if isinstance(value, str):
        return _sanitize_text(value, secret)
    return value


def _sanitize_text(value: str, secret: str) -> str:
    result = value.replace(secret, "[REDACTED]") if secret else value
    return SECRET_PATTERN.sub("[REDACTED]", result)
