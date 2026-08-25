from __future__ import annotations

import json
import unittest
from pathlib import Path

from apiyi_provider import APIYIProvider, decode_raw_response
from apiyi_transport import (
    SanitizedHTTPError,
    TransportPolicyError,
    sanitize_error,
    validate_target,
)
from secret_provider_client import SecretMaterial
from token_platform.models import ProviderFailure


class _Resolver:
    def resolve(self, reference: str) -> SecretMaterial:
        return SecretMaterial(reference, 1, "unit-test-material", "f" * 64)


class _Response:
    def __init__(
        self, body: bytes, status: int = 200, headers: dict[str, str] | None = None
    ):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.peer_ip = "1.1.1.1"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return self.body

    def close(self) -> None:
        return None


class _StreamResponse:
    def __init__(self, frames: list[bytes]):
        self.lines = iter(b"".join(frames).splitlines(keepends=True))
        self.status = 200
        self.headers = {}
        self.peer_ip = "1.1.1.1"

    def readline(self, _limit: int) -> bytes:
        return next(self.lines, b"")

    def close(self) -> None:
        return None


class _Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, method, path, secret, *, body=None, accept="application/json"):
        self.calls.append((method, path, secret, body, accept))
        return self.response


class APIYIProviderTests(unittest.TestCase):
    def test_nonstream_preserves_tool_call_response_and_usage(self) -> None:
        upstream = json.dumps(
            {
                "id": "req-upstream",
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": "call-1", "type": "function"}],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
            separators=(",", ":"),
        ).encode()
        transport = _Transport(_Response(upstream))
        provider = APIYIProvider(
            "https://api.apiyi.com/v1",
            _Resolver(),
            "apiyi/integration",
            transport=transport,
        )
        prompt = json.dumps(
            {
                "_ai16t_endpoint": "chat.completions",
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "2+2"}],
                "tools": [{"type": "function", "function": {"name": "calculator"}}],
            }
        )
        result = provider.call(
            supplier_id="apiyi-canary", model="gpt-test", prompt=prompt
        )
        self.assertEqual(
            (result.usage.input_tokens, result.usage.output_tokens), (7, 3)
        )
        self.assertEqual(result.usage.cached_tokens, 2)
        self.assertEqual(decode_raw_response(result.content), upstream)
        self.assertEqual(transport.calls[0][0:2], ("POST", "/v1/chat/completions"))
        self.assertNotIn(
            "unit-test-material", repr(_Resolver().resolve("apiyi/integration"))
        )

    def test_base_url_and_model_binding_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            APIYIProvider("https://api.apiyi.com", _Resolver(), "apiyi/integration")
        provider = APIYIProvider(
            "https://api.apiyi.com/v1",
            _Resolver(),
            "apiyi/integration",
            transport=_Transport(_Response(b"{}")),
        )
        with self.assertRaises(ProviderFailure):
            provider.call(
                supplier_id="apiyi-canary",
                model="expected",
                prompt=json.dumps(
                    {
                        "_ai16t_endpoint": "chat.completions",
                        "model": "different",
                        "messages": [{"role": "user", "content": "x"}],
                    }
                ),
            )

    def test_secret_repr_is_redacted(self) -> None:
        material = SecretMaterial("apiyi/prod", 2, "super-sensitive-value")
        self.assertEqual(material.reveal(), "super-sensitive-value")
        self.assertNotIn("super-sensitive", repr(material))

    def test_stream_emits_complete_frames_and_terminal_after_usage(self) -> None:
        frames = [
            b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n',
            b"data: [DONE]\n\n",
        ]
        provider = APIYIProvider(
            "https://api.apiyi.com/v1",
            _Resolver(),
            "apiyi/integration",
            transport=_Transport(_StreamResponse(frames)),
        )
        prompt = json.dumps(
            {
                "_ai16t_endpoint": "chat.completions",
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "count"}],
                "stream": True,
            }
        )
        chunks = list(
            provider.stream(supplier_id="apiyi-canary", model="gpt-test", prompt=prompt)
        )

        self.assertEqual([chunk.content.encode() for chunk in chunks], frames)
        self.assertEqual(
            [
                (chunk.usage_delta.input_tokens, chunk.usage_delta.output_tokens)
                for chunk in chunks
            ],
            [(0, 0), (7, 3), (0, 0)],
        )
        self.assertEqual(
            [chunk.finish_reason for chunk in chunks], [None, None, "stop"]
        )

    def test_responses_completed_frame_is_terminal_and_carries_usage(self) -> None:
        frames = [
            b'data: {"type":"response.output_text.delta","delta":"one"}\n\n',
            b'data: {"type":"response.incomplete","response":{"usage":{"input_tokens":35,"output_tokens":240}}}\n\n',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":9,"output_tokens":4}}}\n\n',
        ]
        provider = APIYIProvider(
            "https://api.apiyi.com/v1",
            _Resolver(),
            "apiyi/integration",
            transport=_Transport(_StreamResponse(frames)),
        )
        prompt = json.dumps(
            {
                "_ai16t_endpoint": "responses",
                "model": "gpt-test",
                "input": "count",
                "stream": True,
            }
        )
        chunks = list(
            provider.stream(supplier_id="apiyi-canary", model="gpt-test", prompt=prompt)
        )

        self.assertEqual([chunk.content.encode() for chunk in chunks], frames)
        self.assertEqual(chunks[-1].finish_reason, "stop")
        self.assertEqual(
            (chunks[-2].usage_delta.input_tokens, chunks[-2].usage_delta.output_tokens),
            (0, 0),
        )
        self.assertEqual(
            (chunks[-1].usage_delta.input_tokens, chunks[-1].usage_delta.output_tokens),
            (9, 4),
        )

    def test_url_secret_and_non_allowlisted_targets_fail_closed(self) -> None:
        rejected = (
            ("https://api.apiyi.com", "/v1/models?token=forbidden"),
            ("http://api.apiyi.com", "/v1/models"),
            ("https://user:pass@api.apiyi.com", "/v1/models"),
            ("https://example.com", "/v1/models"),
            ("https://api.apiyi.com", "/v1/unknown"),
            ("https://api.apiyi.com", "/v1/models#secret"),
        )
        for base_url, path in rejected:
            with (
                self.subTest(base_url=base_url, path=path),
                self.assertRaises(TransportPolicyError),
            ):
                validate_target(base_url, path)

    def test_sanitized_error_removes_secret_and_sensitive_fields(self) -> None:
        secret = "unit-test-private-material"
        response = _Response(
            json.dumps(
                {
                    "error": {
                        "code": "invalid_key",
                        "message": "rejected " + secret,
                        "token": secret,
                    }
                }
            ).encode(),
            status=401,
            headers={"Content-Type": "application/json", "x-request-id": "req-safe"},
        )
        result = sanitize_error(response, secret)
        self.assertEqual(
            result,
            SanitizedHTTPError(
                401,
                "application/json",
                "req-safe",
                "invalid_key",
                "rejected [REDACTED]",
            ),
        )
        self.assertNotIn(secret, repr(result))

    def test_auth_probe_records_success_headers_without_returning_body(self) -> None:
        response = _Response(
            b'{"data":[{"id":"deepseek-chat"}]}',
            status=200,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-request-id": "req-safe-models",
            },
        )
        provider = APIYIProvider(
            "https://api.apiyi.com/v1",
            _Resolver(),
            "apiyi/integration",
            transport=_Transport(response),
        )
        result = provider.probe_models()
        self.assertEqual(result["content_type"], "application/json")
        self.assertEqual(result["x_request_id"], "req-safe-models")
        self.assertEqual(
            result["upstream_error_body_sanitized"], {"code": "", "message": ""}
        )
        self.assertNotIn("deepseek-chat", repr(result))

    def test_auth_probe_sanitizes_secret_reflected_in_success_headers(self) -> None:
        secret = "unit-test-material"
        response = _Response(
            b'{"data":[]}',
            status=200,
            headers={
                "Content-Type": "application/" + secret,
                "x-request-id": "Bearer " + secret,
            },
        )
        provider = APIYIProvider(
            "https://api.apiyi.com/v1",
            _Resolver(),
            "apiyi/integration",
            transport=_Transport(response),
        )
        result = provider.probe_models()
        self.assertEqual(result["content_type"], "")
        self.assertNotIn(secret, repr(result))


class DockerBuildContextTests(unittest.TestCase):
    def test_nested_runtime_directories_are_excluded_from_build_context(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        rules = {
            line.strip()
            for line in (repository / ".dockerignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("**/.runtime/", rules)


if __name__ == "__main__":
    unittest.main()
