from __future__ import annotations

import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
