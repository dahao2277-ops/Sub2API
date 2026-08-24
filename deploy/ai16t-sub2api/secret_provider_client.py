from __future__ import annotations

import http.client
import json
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

MAX_SECRET_RESPONSE = 32 * 1024


class SecretProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class SecretMaterial:
    reference: str
    version: int
    _value: str

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return (
            f"SecretMaterial(reference={self.reference!r}, version={self.version}, "
            "value='[REDACTED]')"
        )


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


class UnixSecretResolver:
    """Resolve one allowlisted reference over a private mode-0600 Unix socket."""

    def __init__(self, socket_path: str, allowed_references: set[str], timeout: float = 2.0):
        self.socket_path = str(Path(socket_path))
        self.allowed_references = frozenset(allowed_references)
        self.timeout = timeout

    def ready(self) -> None:
        self._validate_socket()
        connection = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            connection.request("GET", "/health", headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(MAX_SECRET_RESPONSE + 1)
            if response.status != 200 or len(body) > MAX_SECRET_RESPONSE:
                raise SecretProviderUnavailable("secret provider is unavailable")
        except (OSError, http.client.HTTPException) as error:
            raise SecretProviderUnavailable("secret provider is unavailable") from error
        finally:
            connection.close()

    def resolve(self, reference: str) -> SecretMaterial:
        if reference not in self.allowed_references:
            raise SecretProviderUnavailable("secret reference is unavailable")
        self._validate_socket()
        connection = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            connection.request(
                "GET",
                "/v1/secrets/" + quote(reference, safe=""),
                headers={"Accept": "application/json"},
            )
            response = connection.getresponse()
            body = response.read(MAX_SECRET_RESPONSE + 1)
            if response.status != 200 or len(body) > MAX_SECRET_RESPONSE:
                raise SecretProviderUnavailable("secret reference is unavailable")
            payload = json.loads(body)
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as error:
            raise SecretProviderUnavailable("secret provider is unavailable") from error
        finally:
            connection.close()
        if (
            not isinstance(payload, dict)
            or payload.get("secret_ref") != reference
            or not isinstance(payload.get("version"), int)
            or payload["version"] < 1
            or not isinstance(payload.get("value"), str)
            or not payload["value"]
        ):
            raise SecretProviderUnavailable("secret provider response is invalid")
        return SecretMaterial(reference, payload["version"], payload["value"])

    def _validate_socket(self) -> None:
        try:
            details = os.stat(self.socket_path, follow_symlinks=False)
        except OSError as error:
            raise SecretProviderUnavailable("secret provider is unavailable") from error
        if not stat.S_ISSOCK(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
            raise SecretProviderUnavailable("secret provider socket is insecure")
