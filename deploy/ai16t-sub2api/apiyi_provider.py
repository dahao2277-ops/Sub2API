from __future__ import annotations

import base64
import json
import time
from collections.abc import Generator, Iterator
from typing import Any
from urllib.parse import urlsplit

from apiyi_transport import (
    APIYITransport,
    SanitizedHTTPError,
    assert_secret_absent,
    sanitize_error,
    sanitize_response_metadata,
)
from secret_provider_client import UnixSecretResolver
from token_platform.models import ProviderFailure, ProviderResult, StreamChunk, Usage

RAW_RESPONSE_PREFIX = "ai16t-raw-v1:"
MAX_UPSTREAM_RESPONSE = 4 * 1024 * 1024
MAX_SSE_LINE = 512 * 1024
MAX_SSE_FRAME = 1024 * 1024


class APIYIProvider:
    """Narrow OpenAI-compatible adapter with a SecretProvider-only credential path."""

    def __init__(
        self,
        base_url: str,
        secret_resolver: UnixSecretResolver,
        secret_reference: str,
        timeout_seconds: float = 20.0,
        transport: APIYITransport | None = None,
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
        self.transport = transport or APIYITransport(timeout_seconds=timeout_seconds)

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
            raise ProviderFailure(
                "test_hook_disabled", retryable=False, usage_source="confirmed_none"
            )
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
            raise ProviderFailure(
                "test_hook_disabled", retryable=False, usage_source="confirmed_none"
            )
        endpoint, payload = self._payload(prompt, model, stream=True)
        try:
            response = self._open_response(endpoint, payload)
        except ProviderFailure:
            raise
        except (TimeoutError, OSError) as error:
            raise ProviderFailure("provider_timeout", usage_source="missing") from error

        cumulative = Usage(0, 0, 0)
        usage_seen = False
        total_bytes = 0
        frame = bytearray()
        try:
            while True:
                line = response.readline(MAX_SSE_LINE + 1)
                if not line:
                    if frame:
                        usage_seen, cumulative = yield from self._emit_frame(
                            bytes(frame), endpoint, cumulative, usage_seen
                        )
                    break
                total_bytes += len(line)
                if len(line) > MAX_SSE_LINE or total_bytes > MAX_UPSTREAM_RESPONSE:
                    raise ProviderFailure(
                        "provider_response_too_large",
                        retryable=False,
                        usage_source="missing",
                    )
                frame.extend(line)
                if len(frame) > MAX_SSE_FRAME:
                    raise ProviderFailure(
                        "provider_response_too_large",
                        retryable=False,
                        usage_source="missing",
                    )
                if line not in (b"\n", b"\r\n"):
                    continue
                if not bytes(frame).strip():
                    frame.clear()
                    continue
                usage_seen, cumulative = yield from self._emit_frame(
                    bytes(frame), endpoint, cumulative, usage_seen
                )
                frame.clear()
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

    def _emit_frame(
        self,
        frame: bytes,
        endpoint: str,
        cumulative: Usage,
        usage_seen: bool,
    ) -> Generator[StreamChunk, None, tuple[bool, Usage]]:
        usage = self._sse_usage(frame, endpoint)
        delta = Usage(0, 0, 0)
        if usage is not None:
            if (
                usage.input_tokens < cumulative.input_tokens
                or usage.output_tokens < cumulative.output_tokens
                or usage.cached_tokens < cumulative.cached_tokens
            ):
                raise ProviderFailure(
                    "provider_usage_regressed", retryable=False, usage_source="invalid"
                )
            delta = Usage(
                usage.input_tokens - cumulative.input_tokens,
                usage.output_tokens - cumulative.output_tokens,
                usage.cached_tokens - cumulative.cached_tokens,
            )
            cumulative = usage
            usage_seen = True
        finish_reason = self._sse_finish_reason(frame, endpoint)
        yield StreamChunk(frame.decode("utf-8"), delta, finish_reason)
        return usage_seen, cumulative

    def _payload(
        self, prompt: str, model: str, *, stream: bool
    ) -> tuple[str, dict[str, Any]]:
        try:
            payload = json.loads(prompt)
        except json.JSONDecodeError as error:
            raise ProviderFailure(
                "provider_payload_invalid",
                retryable=False,
                usage_source="confirmed_none",
            ) from error
        if not isinstance(payload, dict):
            raise ProviderFailure(
                "provider_payload_invalid",
                retryable=False,
                usage_source="confirmed_none",
            )
        endpoint = payload.pop("_ai16t_endpoint", "chat.completions")
        if (
            endpoint not in ("chat.completions", "responses")
            or payload.get("model") != model
        ):
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
                    "provider_payload_invalid",
                    retryable=False,
                    usage_source="confirmed_none",
                )
            options["include_usage"] = True
            payload["stream_options"] = options
        return str(endpoint), payload

    def _request(self, endpoint: str, payload: dict[str, Any]) -> bytes:
        try:
            with self._open_response(endpoint, payload) as response:
                body = response.read(MAX_UPSTREAM_RESPONSE + 1)
        except ProviderFailure:
            raise
        except (TimeoutError, OSError) as error:
            raise ProviderFailure("provider_timeout", usage_source="missing") from error
        if len(body) > MAX_UPSTREAM_RESPONSE:
            raise ProviderFailure(
                "provider_response_too_large", retryable=False, usage_source="missing"
            )
        return body

    def _open_response(self, endpoint: str, payload: dict[str, Any]) -> Any:
        material = self.secret_resolver.resolve(self.secret_reference)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        path = (
            "/v1/chat/completions"
            if endpoint == "chat.completions"
            else "/v1/responses"
        )
        secret = material.reveal()
        try:
            response = self.transport.open(
                "POST",
                path,
                secret,
                body=body,
                accept="text/event-stream"
                if payload.get("stream")
                else "application/json",
            )
            if 200 <= response.status < 300:
                return response
            evidence = sanitize_error(response, secret)
            response.close()
            raise self._http_failure(evidence)
        finally:
            # Best-effort local reference release.
            secret = ""  # nosec B105

    def probe_models(self) -> dict[str, Any]:
        material = self.secret_resolver.resolve(self.secret_reference)
        secret = material.reveal()
        response = None
        try:
            response = self.transport.open("GET", "/v1/models", secret)
            if 200 <= response.status < 300:
                response.read(64 * 1024 + 1)
                evidence = sanitize_response_metadata(response, secret)
            else:
                evidence = sanitize_error(response, secret)
            result = {
                "endpoint": "https://api.apiyi.com/v1/models",
                "upstream_http_status": evidence.status,
                "content_type": evidence.content_type,
                "x_request_id": evidence.request_id,
                "upstream_error_code": evidence.error_code,
                "upstream_error_message": evidence.error_message,
                "upstream_error_body_sanitized": {
                    "code": evidence.error_code,
                    "message": evidence.error_message,
                },
                "key_fingerprint": material.fingerprint,
                "authorization_header_present": True,
                "bearer_prefix_correct": True,
                "authorization_header_length": len(secret) + len("Bearer "),
                "url_contains_secret": False,
                "redirect_followed": False,
                "peer_ip": response.peer_ip,
            }
            assert_secret_absent(result, secret)
            return result
        finally:
            if response is not None:
                response.close()
            # Best-effort local reference release.
            secret = ""  # nosec B105

    @staticmethod
    def _http_failure(error: SanitizedHTTPError) -> ProviderFailure:
        status = error.status
        if status in (401, 403):
            return ProviderFailure(
                "provider_auth_rejected", retryable=False, usage_source="confirmed_none"
            )
        if status == 429:
            return ProviderFailure(
                "provider_rate_limited", retryable=True, usage_source="confirmed_none"
            )
        if 400 <= status < 500:
            return ProviderFailure(
                "provider_request_rejected",
                retryable=False,
                usage_source="confirmed_none",
            )
        return ProviderFailure(
            "provider_upstream_error", retryable=True, usage_source="missing"
        )

    @staticmethod
    def _usage(payload: dict[str, Any], endpoint: str) -> Usage:
        source = payload.get("usage")
        if endpoint == "responses" and not isinstance(source, dict):
            response = payload.get("response")
            source = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(source, dict):
            raise TypeError("usage is missing")
        if endpoint == "chat.completions":
            input_tokens = int(source["prompt_tokens"])
            output_tokens = int(source["completion_tokens"])
            details = source.get("prompt_tokens_details")
        else:
            input_tokens = int(source["input_tokens"])
            output_tokens = int(source["output_tokens"])
            details = source.get("input_tokens_details")
        cached = (
            int(details.get("cached_tokens", 0)) if isinstance(details, dict) else 0
        )
        usage = Usage(input_tokens, output_tokens, cached)
        usage.validate()
        return usage

    @classmethod
    def _sse_usage(cls, line: bytes, endpoint: str) -> Usage | None:
        payload = cls._sse_payload(line)
        if payload is None:
            return None
        # APIYI may emit response.incomplete snapshots whose token counters are
        # estimates and can be greater than the authoritative completed Usage.
        # Billing accepts only the final response.completed event.
        if endpoint == "responses" and payload.get("type") != "response.completed":
            return None
        try:
            return cls._usage(payload, endpoint)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _sse_data(frame: bytes) -> bytes | None:
        values = []
        for line in frame.splitlines():
            if line.startswith(b"data:"):
                values.append(line[5:].lstrip())
        if not values:
            return None
        return b"\n".join(values).strip()

    @classmethod
    def _sse_payload(cls, frame: bytes) -> dict[str, Any] | None:
        value = cls._sse_data(frame)
        if not value or value == b"[DONE]":
            return None
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _sse_finish_reason(cls, frame: bytes, endpoint: str) -> str | None:
        if cls._sse_data(frame) == b"[DONE]":
            return "stop"
        payload = cls._sse_payload(frame)
        if payload is None:
            return None
        if endpoint == "chat.completions":
            # Chat's choices finish_reason precedes the include_usage frame.
            # Only [DONE] is terminal; otherwise holding this frame until after
            # settlement would reorder it behind the final usage event.
            return None
        event_type = payload.get("type")
        return (
            "stop" if event_type in ("response.completed", "response.failed") else None
        )

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
