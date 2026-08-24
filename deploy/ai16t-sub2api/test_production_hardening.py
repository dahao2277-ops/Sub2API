from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from apiyi_provider import APIYIProvider, decode_raw_response
from secret_provider_client import SecretMaterial


class _Resolver:
    def resolve(self, reference: str) -> SecretMaterial:
        return SecretMaterial(reference, 1, "sk-fixture-not-a-real-key")


class _Response:
    def __init__(self, body: bytes):
        self.body = body

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

    def readline(self, _limit: int) -> bytes:
        return next(self.lines, b"")

    def close(self) -> None:
        return None


class APIYIProviderTests(unittest.TestCase):
    def test_nonstream_preserves_tool_call_response_and_usage(self) -> None:
        provider = APIYIProvider(
            "https://api.apiyi.com/v1", _Resolver(), "apiyi/integration"
        )
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
        prompt = json.dumps(
            {
                "_ai16t_endpoint": "chat.completions",
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "2+2"}],
                "tools": [{"type": "function", "function": {"name": "calculator"}}],
            }
        )
        with patch("urllib.request.urlopen", return_value=_Response(upstream)):
            result = provider.call(supplier_id="apiyi-canary", model="gpt-test", prompt=prompt)
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (7, 3))
        self.assertEqual(result.usage.cached_tokens, 2)
        self.assertEqual(decode_raw_response(result.content), upstream)
        self.assertNotIn("sk-fixture", repr(_Resolver().resolve("apiyi/integration")))

    def test_base_url_and_model_binding_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            APIYIProvider("https://api.apiyi.com", _Resolver(), "apiyi/integration")
        provider = APIYIProvider(
            "https://api.apiyi.com/v1", _Resolver(), "apiyi/integration"
        )
        with self.assertRaises(Exception):
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
        material = SecretMaterial("apiyi/prod", 2, "sk-super-sensitive")
        self.assertEqual(material.reveal(), "sk-super-sensitive")
        self.assertNotIn("super-sensitive", repr(material))

    def test_stream_emits_complete_frames_and_terminal_after_usage(self) -> None:
        provider = APIYIProvider(
            "https://api.apiyi.com/v1", _Resolver(), "apiyi/integration"
        )
        frames = [
            b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n',
            b"data: [DONE]\n\n",
        ]
        prompt = json.dumps(
            {
                "_ai16t_endpoint": "chat.completions",
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "count"}],
                "stream": True,
            }
        )
        with patch("urllib.request.urlopen", return_value=_StreamResponse(frames)):
            chunks = list(
                provider.stream(
                    supplier_id="apiyi-canary", model="gpt-test", prompt=prompt
                )
            )

        self.assertEqual([chunk.content.encode() for chunk in chunks], frames)
        self.assertEqual(
            [
                (chunk.usage_delta.input_tokens, chunk.usage_delta.output_tokens)
                for chunk in chunks
            ],
            [(0, 0), (7, 3), (0, 0)],
        )
        self.assertEqual([chunk.finish_reason for chunk in chunks], [None, None, "stop"])

    def test_responses_completed_frame_is_terminal_and_carries_usage(self) -> None:
        provider = APIYIProvider(
            "https://api.apiyi.com/v1", _Resolver(), "apiyi/integration"
        )
        frames = [
            b'data: {"type":"response.output_text.delta","delta":"one"}\n\n',
            b'data: {"type":"response.incomplete","response":{"usage":{"input_tokens":35,"output_tokens":240}}}\n\n',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":9,"output_tokens":4}}}\n\n',
        ]
        prompt = json.dumps(
            {
                "_ai16t_endpoint": "responses",
                "model": "gpt-test",
                "input": "count",
                "stream": True,
            }
        )
        with patch("urllib.request.urlopen", return_value=_StreamResponse(frames)):
            chunks = list(
                provider.stream(
                    supplier_id="apiyi-canary", model="gpt-test", prompt=prompt
                )
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


class DockerBuildContextTests(unittest.TestCase):
    def test_nested_runtime_directories_are_excluded_from_build_context(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        rules = {
            line.strip()
            for line in (repository / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("**/.runtime/", rules)


if __name__ == "__main__":
    unittest.main()
