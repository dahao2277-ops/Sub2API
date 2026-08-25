from __future__ import annotations

import unittest
from pathlib import Path

from apiyi_transport import (
    APIYITransport,
    TransportPolicyError,
    _PinnedHTTPSConnection,
    assert_secret_absent,
    sanitize_response_metadata,
)


class _Socket:
    def __init__(self, peer_ip: str = "1.1.1.1") -> None:
        self.peer_ip = peer_ip

    def getpeername(self) -> tuple[str, int]:
        return (self.peer_ip, 443)


class _HTTPResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}

    def read(self, _amount: int) -> bytes:
        return b"{}"

    def close(self) -> None:
        return None


class _Connection:
    def __init__(
        self,
        status: int = 200,
        *,
        peer_ip: str = "1.1.1.1",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.peer_ip = peer_ip
        self.headers = headers
        self.sock = None
        self.request_record = None
        self.closed = False

    def connect(self) -> None:
        self.sock = _Socket(self.peer_ip)

    def request(self, method, path, body=None, headers=None) -> None:
        self.request_record = (method, path, body, dict(headers or {}))

    def getresponse(self) -> _HTTPResponse:
        return _HTTPResponse(self.status, self.headers)

    def close(self) -> None:
        self.closed = True


class AuthTransportClearanceTests(unittest.TestCase):
    def test_secret_is_only_in_authorization_header(self) -> None:
        connection = _Connection()
        transport = APIYITransport(
            resolver=lambda _host: ("1.1.1.1",),
            connection_factory=lambda *_args: connection,
        )
        secret = "diagnostic-material-never-a-real-credential"
        response = transport.open("GET", "/v1/models", secret)
        response.close()
        method, path, body, headers = connection.request_record
        self.assertEqual((method, path, body), ("GET", "/v1/models", None))
        self.assertNotIn(secret, path)
        self.assertNotIn(
            secret,
            repr(
                {key: value for key, value in headers.items() if key != "Authorization"}
            ),
        )
        self.assertEqual(headers["Authorization"], "Bearer " + secret)

    def test_redirect_is_fail_closed_and_never_followed(self) -> None:
        connection = _Connection(status=302)
        transport = APIYITransport(
            resolver=lambda _host: ("1.1.1.1",),
            connection_factory=lambda *_args: connection,
        )
        with self.assertRaisesRegex(TransportPolicyError, "redirect rejected"):
            transport.open("GET", "/v1/models", "diagnostic-material")
        self.assertTrue(connection.closed)

    def test_mixed_public_private_dns_is_rejected(self) -> None:
        transport = APIYITransport(
            resolver=lambda _host: ("1.1.1.1", "127.0.0.1"),
            connection_factory=lambda *_args: _Connection(),
        )
        with self.assertRaisesRegex(TransportPolicyError, "outside the public"):
            transport.open("GET", "/v1/models", "diagnostic-material")

    def test_peer_mismatch_is_rejected(self) -> None:
        connection = _Connection(peer_ip="8.8.8.8")
        transport = APIYITransport(
            resolver=lambda _host: ("1.1.1.1",),
            connection_factory=lambda *_args: connection,
        )
        with self.assertRaisesRegex(TransportPolicyError, "target changed"):
            transport.open("GET", "/v1/models", "diagnostic-material")

    def test_proxy_tunnel_is_rejected_before_socket_creation(self) -> None:
        connection = _PinnedHTTPSConnection("api.apiyi.com", "1.1.1.1", 1)
        connection.set_tunnel("proxy.invalid")
        with self.assertRaisesRegex(TransportPolicyError, "proxy tunnels"):
            connection.connect()

    def test_success_metadata_sanitizes_reflected_secret_and_content_type(self) -> None:
        secret = "diagnostic-material-never-a-real-credential"
        connection = _Connection(
            headers={
                "Content-Type": "application/" + secret,
                "x-request-id": "reflected-Bearer " + secret,
            }
        )
        transport = APIYITransport(
            resolver=lambda _host: ("1.1.1.1",),
            connection_factory=lambda *_args: connection,
        )
        response = transport.open("GET", "/v1/models", secret)
        evidence = sanitize_response_metadata(response, secret)
        response.close()
        self.assertEqual(evidence.content_type, "")
        self.assertNotIn(secret, evidence.request_id)
        assert_secret_absent(evidence, secret)

    def test_success_metadata_allows_canonical_json_content_type(self) -> None:
        secret = "diagnostic-material-never-a-real-credential"
        connection = _Connection(
            headers={"Content-Type": "Application/JSON; charset=utf-8"}
        )
        transport = APIYITransport(
            resolver=lambda _host: ("1.1.1.1",),
            connection_factory=lambda *_args: connection,
        )
        response = transport.open("GET", "/v1/models", secret)
        evidence = sanitize_response_metadata(response, secret)
        response.close()
        self.assertEqual(evidence.content_type, "application/json")

    def test_production_upstream_modules_do_not_use_urlopen_or_secret_query_names(
        self,
    ) -> None:
        root = Path(__file__).resolve().parent
        names = (
            "apiyi_transport.py",
            "apiyi_provider.py",
            "apiyi_catalog.py",
            "apiyi_auth_probe.py",
        )
        forbidden = (
            "urllib.request.urlopen",
            "?api_key=",
            "?token=",
            "?access_token=",
            "#access_token=",
        )
        for name in names:
            source = (root / name).read_text(encoding="utf-8")
            for marker in forbidden:
                with self.subTest(name=name, marker=marker):
                    self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
