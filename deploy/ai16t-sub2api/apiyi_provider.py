from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from token_platform.models import ProviderFailure, ProviderResult, StreamChunk, Usage

from secret_provider_client import UnixSecretResolver

RAW_RESPONSE_PREFIX = "ai16t-raw-v1:"
MAX_UPSTREAM_RESPONSE = 4 * 1024 * 1024
MAX_SSE_LINE = 512 * 1024


class APIYIProvider:
    """Narrow OpenAI-compatible adapter with a SecretProvider-only credential path."""

    def __init__(
        self,
        base_url: str,
        secret_resolver: UnixSecretResolver,
        secret_reference: str,
        timeout_seconds: float = 20.0,
    ):
        parsed = urlsplit(base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.apiyi.com"
            or parsed.port is not None
            or parsed.path != "/v1"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("APIYI base URL must be exactly https://api.apiyi.com/v1")
        self.base_url = base_url.rstrip("/")
        self.secret_resolver = secret_resolver
        self.secret_reference = secret_reference
        self.timeout_seconds = timeout_seconds

    def call(
        self,
        *,
        supplier_id: str,
        model: str,
        prompt: str,
        failure: str | None = None,
        stream: bool = False,
    ) -> ProviderResult:
        del stream
        if failure:
            raise ProviderFailure("test_hook_disabled", retryable=False, usage_source="confirmed_none")
        endpoint, payload = self._payload(prompt, model, stream=False)
        started = time.monotonic()
        body = self._request(endpoint, payload)
        try:
            response = json.loads(body)
            usage = self._usage(response, endpoint)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProviderFailure(
                "provider_invalid_response", retryable=False, usage_source="missing"
            ) from error
        return ProviderResult(
            supplier_id=supplier_id,
            usage=usage,
            content=RAW_RESPONSE_PREFIX + base64.b64encode(body).decode("ascii"),
            latency_ms=max(1, int((time.monotonic() - started) * 1000)),
            finish_reason=self._finish_reason(response, endpoint),
        )

    def stream(
        self,
        *,
        supplier_id: str,
        model: str,
        prompt: str,
        failure: str | None = None,
    ) -> Iterator[StreamChunk]:
        del supplier_id
        if failure:
            raise ProviderFailure("test_hook_disabled", retryable=False, usage_source="confirmed_none")
        endpoint, payload = self._payload(prompt, model, stream=True)
        request = self._build_request(endpoint, payload)
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as error:
            raise self._http_failure(error) from error
        except (TimeoutError, urllib.error.URLError) as error:
            raise ProviderFailure("provider_timeout", usage_source="missing") from error

        cumulative = Usage(0, 0, 0)
        usage_seen = False
        total_bytes = 0
        try:
            while True:
                line = response.readline(MAX_SSE_LINE + 1)
                if not line:
                    break
                total_bytes += len(line)
                if len(line) > MAX_SSE_LINE or total_bytes > MAX_UPSTREAM_RESPONSE:
                    raise ProviderFailure(
                        "provider_response_too_large", retryable=False, usage_source="missing"
                    )
                usage = self._sse_usage(line, endpoint)
                delta = Usage(0, 0, 0)
                if usage is not None:
                    if (
                        usage.input_tokens < cumulative.input_tokens
                        or usage.output_tokens < cumulative.output_tokens
                        or usage.cached_tokens < cumulative.cached_tokens
                    ):
                        raise ProviderFailure(
                            "provider_usage_regressed",
                            retryable=False,
                            usage_source="invalid",
                        )
                    delta = Usage(
                        usage.input_tokens - cumulative.input_tokens,
                        usage.output_tokens - cumulative.output_tokens,
                        usage.cached_tokens - cumulative.cached_tokens,
                    )
                    cumulative = usage
                    usage_seen = True
                finish_reason = self._sse_finish_reason(line, endpoint)
                yield StreamChunk(line.decode("utf-8"), delta, finish_reason)
        except UnicodeDecodeError as error:
            raise ProviderFailure(
                "provider_invalid_stream", retryable=False, usage_source="missing"
            ) from error
        finally:
            response.close()
        if not usage_seen:
            raise ProviderFailure(
                "provider_stream_usage_missing", retryable=False, usage_source="missing"
            )

    def _payload(self, prompt: str, model: str, *, stream: bool) -> tuple[str, dict[str, Any]]:
        try:
            payload = json.loads(prompt)
        except json.JSONDecodeError as error:
            raise ProviderFailure(
                "provider_payload_invalid", retryable=False, usage_source="confirmed_none"
            ) from error
        if not isinstance(payload, dict):
            raise ProviderFailure(
                "provider_payload_invalid", retryable=False, usage_source="confirmed_none"
            )
        endpoint = payload.pop("_ai16t_endpoint", "chat.completions")
        if endpoint not in ("chat.completions", "responses") or payload.get("model") != model:
            raise ProviderFailure(
                "provider_model_binding_invalid",
                retryable=False,
                usage_source="confirmed_none",
            )
        payload["stream"] = stream
        if endpoint == "chat.completions" and stream:
            options = payload.get("stream_options")
            if options is None:
                options = {}
            if not isinstance(options, dict):
                raise ProviderFailure(
                    "provider_payload_invalid", retryable=False, usage_source="confirmed_none"
                )
            options["include_usage"] = True
            payload["stream_options"] = options
        return str(endpoint), payload

    def _request(self, endpoint: str, payload: dict[str, Any]) -> bytes:
        request = self._build_request(endpoint, payload)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(MAX_UPSTREAM_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            raise self._http_failure(error) from error
        except (TimeoutError, urllib.error.URLError) as error:
            raise ProviderFailure("provider_timeout", usage_source="missing") from error
        if len(body) > MAX_UPSTREAM_RESPONSE:
            raise ProviderFailure(
                "provider_response_too_large", retryable=False, usage_source="missing"
            )
        return body

    def _build_request(self, endpoint: str, payload: dict[str, Any]) -> urllib.request.Request:
        material = self.secret_resolver.resolve(self.secret_reference)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        path = "/chat/completions" if endpoint == "chat.completions" else "/responses"
        return urllib.request.Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + material.reveal(),
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if payload.get("stream") else "application/json",
                "User-Agent": "AI16T-Platform-B-Canary/1",
            },
        )

    @staticmethod
    def _http_failure(error: urllib.error.HTTPError) -> ProviderFailure:
        status = int(error.code)
        if status in (401, 403):
            return ProviderFailure("provider_auth_rejected", retryable=False, usage_source="confirmed_none")
        if status == 429:
            return ProviderFailure("provider_rate_limited", retryable=True, usage_source="confirmed_none")
        if 400 <= status < 500:
            return ProviderFailure("provider_request_rejected", retryable=False, usage_source="confirmed_none")
        return ProviderFailure("provider_upstream_error", retryable=True, usage_source="missing")

    @staticmethod
    def _usage(payload: dict[str, Any], endpoint: str) -> Usage:
        source = payload.get("usage")
        if endpoint == "responses" and not isinstance(source, dict):
            response = payload.get("response")
            source = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(source, dict):
            raise ValueError("usage is missing")
        if endpoint == "chat.completions":
            input_tokens = int(source["prompt_tokens"])
            output_tokens = int(source["completion_tokens"])
            details = source.get("prompt_tokens_details")
        else:
            input_tokens = int(source["input_tokens"])
            output_tokens = int(source["output_tokens"])
            details = source.get("input_tokens_details")
        cached = int(details.get("cached_tokens", 0)) if isinstance(details, dict) else 0
        usage = Usage(input_tokens, output_tokens, cached)
        usage.validate()
        return usage

    @classmethod
    def _sse_usage(cls, line: bytes, endpoint: str) -> Usage | None:
        payload = cls._sse_payload(line)
        if payload is None:
            return None
        try:
            return cls._usage(payload, endpoint)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _sse_payload(line: bytes) -> dict[str, Any] | None:
        if not line.startswith(b"data:"):
            return None
        value = line[5:].strip()
        if not value or value == b"[DONE]":
            return None
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _sse_finish_reason(cls, line: bytes, endpoint: str) -> str | None:
        if line.startswith(b"data:") and line[5:].strip() == b"[DONE]":
            return "stop"
        payload = cls._sse_payload(line)
        if payload is None:
            return None
        if endpoint == "chat.completions":
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                value = choices[0].get("finish_reason")
                return str(value) if value else None
            return None
        event_type = payload.get("type")
        return "stop" if event_type in ("response.completed", "response.failed") else None

    @staticmethod
    def _finish_reason(payload: dict[str, Any], endpoint: str) -> str:
        if endpoint == "chat.completions":
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                value = choices[0].get("finish_reason")
                return str(value or "stop")
        return "stop"


def decode_raw_response(content: str) -> bytes | None:
    if not content.startswith(RAW_RESPONSE_PREFIX):
        return None
    try:
        return base64.b64decode(content[len(RAW_RESPONSE_PREFIX) :], validate=True)
    except (ValueError, TypeError):
        return None
