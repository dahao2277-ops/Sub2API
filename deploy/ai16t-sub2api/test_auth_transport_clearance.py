from __future__ import annotations

import unittest
from pathlib import Path

from apiyi_transport import APIYITransport, TransportPolicyError


class _Socket:
    def getpeername(self) -> tuple[str, int]:
        return ("1.1.1.1", 443)


class _HTTPResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    def read(self, _amount: int) -> bytes:
        return b"{}"

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.sock = None
        self.request_record = None
        self.closed = False

    def connect(self) -> None:
        self.sock = _Socket()

    def request(self, method, path, body=None, headers=None) -> None:
        self.request_record = (method, path, body, dict(headers or {}))

    def getresponse(self) -> _HTTPResponse:
        return _HTTPResponse(self.status)

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
