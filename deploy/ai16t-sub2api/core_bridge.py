from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from apiyi_provider import APIYIProvider, decode_raw_response
from canary_budget import (
    CanaryBudgetExceeded,
    MAX_CANARY_INPUT_TOKENS,
    MAX_CANARY_OUTPUT_TOKENS,
    ProviderSpendGate,
    StreamBudgetSession,
    enforce_canary_input_ceiling,
    maximum_request_provider_cost_micro,
)
from secret_provider_client import UnixSecretResolver
from token_platform.config import Settings
from token_platform.hybrid_control_plane import FileReferenceResolver
from token_platform.models import ProviderFailure, ProviderResult, StreamChunk, Usage
from token_platform.platform import TokenPlatform

MAX_BODY = 2 * 1024 * 1024
SIGNATURE_SKEW_SECONDS = 30
FIRST_CANARY_MODELS = frozenset({"deepseek-chat", "gpt-5.6-luna"})


def _read_mode_0600(path: str) -> bytes:
    resolved = Path(path)
    material = FileReferenceResolver({"value": resolved}).resolve("value").reveal().encode()
    if len(material) < 32:
        raise RuntimeError("secret material is too short")
    return material


class HTTPMockProvider:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 0.75):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
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
        payload = json.dumps(
            {
                "supplier_id": supplier_id,
                "model": model,
                "prompt": prompt,
                "failure": failure,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            self.base_url + "/v1/invoke",
            data=payload,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read(MAX_BODY))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(MAX_BODY))
            except (json.JSONDecodeError, OSError, ValueError):
                detail = {}
            partial = detail.get("partial_usage")
            usage = (
                Usage(
                    int(partial.get("input_tokens", 0)),
                    int(partial.get("output_tokens", 0)),
                    int(partial.get("cached_tokens", 0)),
                )
                if isinstance(partial, dict)
                else None
            )
            raise ProviderFailure(
                str(detail.get("code", "provider_http_error")),
                retryable=bool(detail.get("retryable", True)),
                partial_usage=usage,
                usage_source=str(detail.get("usage_source", "missing")),
            ) from error
        except (TimeoutError, urllib.error.URLError) as error:
            raise ProviderFailure("timeout", usage_source="confirmed_none") from error
        usage_data = result["usage"]
        return ProviderResult(
            supplier_id=supplier_id,
            usage=Usage(
                int(usage_data["input_tokens"]),
                int(usage_data["output_tokens"]),
                int(usage_data.get("cached_tokens", 0)),
            ),
            content=str(result["content"]),
            latency_ms=int(result["latency_ms"]),
        )

    def stream(
        self,
        *,
        supplier_id: str,
        model: str,
        prompt: str,
        failure: str | None = None,
    ) -> Any:
        result = self.call(
            supplier_id=supplier_id,
            model=model,
            prompt=prompt,
            failure=failure,
        )
        yield StreamChunk(result.content, result.usage, "stop")


class HybridAuthority:
    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.platform = TokenPlatform(self.settings)
        self.platform.initialize()
        self.provider_mode = os.getenv("AI16T_PROVIDER_MODE", "mock")
        self.secret_resolver: UnixSecretResolver | None = None
        self.secret_reference = ""
        self.provider_spend_gate: ProviderSpendGate | None = None
        self.allowed_users = frozenset(
            value.strip()
            for value in os.getenv("AI16T_CANARY_USER_REFERENCES", "").split(",")
            if value.strip()
        )
        self.allowed_models: frozenset[str]
        if self.provider_mode == "mock":
            resolver = FileReferenceResolver(
                {"mock-provider-api-key": Path(os.environ["AI16T_MOCK_PROVIDER_KEY_FILE"])}
            )
            provider_key = resolver.resolve("mock-provider-api-key").reveal()
            external_provider = HTTPMockProvider(
                os.environ["AI16T_MOCK_PROVIDER_URL"], provider_key
            )
            self.allowed_models = frozenset({"gpt-4o-mini"})
            self.ledger_provider = "openai"
        elif self.provider_mode == "apiyi":
            self.secret_reference = os.environ["AI16T_PROVIDER_SECRET_REF"]
            self.secret_resolver = UnixSecretResolver(
                os.environ["AI16T_SECRET_SOCKET"], {self.secret_reference}
            )
            self.secret_resolver.ready()
            external_provider = APIYIProvider(
                os.environ["AI16T_PROVIDER_BASE_URL"],
                self.secret_resolver,
                self.secret_reference,
                float(os.getenv("AI16T_PROVIDER_TIMEOUT_SECONDS", "20")),
            )
            self.ledger_provider = "apiyi"
            self.allowed_models = self._configure_apiyi_catalog(
                os.environ["AI16T_MODEL_CONFIG_FILE"]
            )
            self.provider_spend_gate = ProviderSpendGate(
                self.platform.database,
                limit_micro=int(os.environ["AI16T_PROVIDER_SPEND_LIMIT_MICRO"]),
                daily_limit_micro=int(os.environ["AI16T_DAILY_SPEND_LIMIT_MICRO"]),
                reserve_micro=int(os.environ["AI16T_REQUEST_RESERVE_MICRO"]),
            )
        else:
            raise RuntimeError("unsupported AI16T provider mode")
        self.platform.provider = external_provider
        self.platform.gateway.provider = external_provider
        self.signing_key = _read_mode_0600(os.environ["AI16T_CORE_SIGNING_KEY_FILE"])
        self.identity_lock = threading.Lock()
        self._initialize_bridge_schema()

    def _configure_apiyi_catalog(self, path: str) -> frozenset[str]:
        raw = FileReferenceResolver({"apiyi-model-config": Path(path)}).resolve(
            "apiyi-model-config"
        ).reveal()
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("APIYI model configuration is invalid") from error
        models = config.get("models") if isinstance(config, dict) else None
        if not isinstance(models, list) or len(models) != len(FIRST_CANARY_MODELS):
            raise RuntimeError("APIYI Canary must contain exactly two approved models")
        allowed: set[str] = set()
        configured: list[dict[str, Any]] = []
        for item in models:
            if not isinstance(item, dict):
                raise RuntimeError("APIYI model configuration is invalid")
            model = str(item.get("model", ""))
            upstream_model = str(item.get("upstream_model", ""))
            values = (
                int(item.get("input_per_million_micro", -1)),
                int(item.get("output_per_million_micro", -1)),
                int(item.get("cached_per_million_micro", -1)),
                int(item.get("customer_input_per_million_micro", -1)),
                int(item.get("customer_output_per_million_micro", -1)),
                int(item.get("customer_cached_per_million_micro", -1)),
            )
            if (
                not model
                or model != upstream_model
                or model in allowed
                or min(values) < 0
                or values[3] < values[0]
                or values[4] < values[1]
                or values[5] < values[2]
            ):
                raise RuntimeError("APIYI model configuration violates pricing safety")
            if maximum_request_provider_cost_micro(*values[:3]) >= 250_000:
                raise RuntimeError(
                    "APIYI model maximum provider request cost must stay below reserve"
                )
            allowed.add(model)
            configured.append({"model": model, "rates": values})

        if allowed != FIRST_CANARY_MODELS:
            raise RuntimeError("APIYI Canary contains an unapproved model")

        with self.platform.database.transaction() as connection:
            placeholders = ",".join("?" for _ in allowed)
            connection.execute(
                f"UPDATE routes SET enabled=0 WHERE provider='apiyi' "
                f"AND model NOT IN ({placeholders})",
                tuple(sorted(allowed)),
            )
        for item in configured:
            model = str(item["model"])
            supplier_input, supplier_output, supplier_cached, customer_input, customer_output, customer_cached = item["rates"]
            self._ensure_price_version(
                "apiyi", model, customer_input, customer_output, customer_cached
            )
            self._ensure_supplier_cost_version(
                "apiyi-canary",
                "apiyi",
                model,
                supplier_input,
                supplier_output,
                supplier_cached,
            )
            with self.platform.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO routes(supplier_id,provider,model,upstream_model,
                    endpoint,priority,weight,cost_input,cost_output,cached_cost,health,enabled)
                    VALUES ('apiyi-canary','apiyi',?,?,?,10,100,?,?,?,'healthy',1)
                    ON CONFLICT(supplier_id,model) DO UPDATE SET
                    upstream_model=excluded.upstream_model,endpoint=excluded.endpoint,
                    priority=excluded.priority,weight=excluded.weight,
                    cost_input=excluded.cost_input,cost_output=excluded.cost_output,
                    cached_cost=excluded.cached_cost,health='healthy',enabled=1""",
                    (
                        model,
                        model,
                        "mock://apiyi-canary",
                        supplier_input,
                        supplier_output,
                        supplier_cached,
                    ),
                )
        return frozenset(allowed)

    def _ensure_price_version(
        self, provider: str, model: str, input_rate: int, output_rate: int, cached_rate: int
    ) -> None:
        with self.platform.database.read() as connection:
            current = connection.execute(
                """SELECT input_per_million_micro,output_per_million_micro,
                cached_per_million_micro FROM price_versions
                WHERE provider=? AND model=? AND active=1 ORDER BY id DESC LIMIT 1""",
                (provider, model),
            ).fetchone()
        if current and tuple(int(value) for value in current) == (
            input_rate,
            output_rate,
            cached_rate,
        ):
            return
        self.platform.create_price_version(provider, model, input_rate, output_rate, cached_rate)

    def _ensure_supplier_cost_version(
        self,
        supplier_id: str,
        provider: str,
        model: str,
        input_rate: int,
        output_rate: int,
        cached_rate: int,
    ) -> None:
        with self.platform.database.read() as connection:
            current = connection.execute(
                """SELECT input_per_million_micro,output_per_million_micro,
                cached_per_million_micro FROM supplier_cost_versions
                WHERE supplier_id=? AND model=? AND active=1 ORDER BY id DESC LIMIT 1""",
                (supplier_id, model),
            ).fetchone()
        if current and tuple(int(value) for value in current) == (
            input_rate,
            output_rate,
            cached_rate,
        ):
            return
        self.platform.create_supplier_cost_version(
            supplier_id, provider, model, input_rate, output_rate, cached_rate
        )

    def _initialize_bridge_schema(self) -> None:
        with self.platform.database.transaction() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sub2api_identity_map(
                external_user_reference TEXT NOT NULL,
                external_key_reference TEXT NOT NULL,
                core_user_id INTEGER NOT NULL REFERENCES users(id),
                core_api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(external_user_reference,external_key_reference))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sub2api_user_authority(
                external_user_reference TEXT PRIMARY KEY,
                core_user_id INTEGER NOT NULL REFERENCES users(id),
                core_api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO sub2api_user_authority(
                external_user_reference,core_user_id,core_api_key_id)
                SELECT legacy.external_user_reference,legacy.core_user_id,legacy.core_api_key_id
                FROM sub2api_identity_map legacy
                WHERE legacy.rowid=(
                    SELECT MIN(first_mapping.rowid) FROM sub2api_identity_map first_mapping
                    WHERE first_mapping.external_user_reference=legacy.external_user_reference)"""
            )
            divergent = connection.execute(
                """SELECT external_user_reference FROM sub2api_identity_map
                GROUP BY external_user_reference
                HAVING COUNT(DISTINCT core_user_id || ':' || core_api_key_id) > 1
                LIMIT 1"""
            ).fetchone()
            if divergent:
                raise RuntimeError(
                    "legacy Sub2API account has divergent Core authorities; "
                    "explicit Ledger migration is required"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS bridge_nonces(
                nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS bridge_requests(
                request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"""
            )

    def authorize(self, method: str, path: str, body: bytes, headers: Any) -> bool:
        timestamp = headers.get("X-AI16T-Timestamp", "")
        nonce = headers.get("X-AI16T-Nonce", "")
        signature = headers.get("X-AI16T-Signature", "")
        try:
            timestamp_value = int(timestamp)
        except ValueError:
            return False
        now = int(time.time())
        if abs(now - timestamp_value) > SIGNATURE_SKEW_SECONDS or len(nonce) != 48:
            return False
        digest = hashlib.sha256(body).hexdigest()
        canonical = "\n".join((method, path, timestamp, nonce, digest)).encode()
        expected = hmac.new(self.signing_key, canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False
        try:
            with self.platform.database.transaction() as connection:
                connection.execute("DELETE FROM bridge_nonces WHERE expires_at < ?", (now,))
                connection.execute(
                    "INSERT INTO bridge_nonces(nonce,expires_at) VALUES (?,?)",
                    (nonce, now + SIGNATURE_SKEW_SECONDS * 2),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def _identity(self, external_user: str, external_key: str) -> tuple[int, int]:
        if not external_user or not external_key:
            raise ValueError("external identity is required")
        if self.provider_mode == "apiyi" and external_user not in self.allowed_users:
            raise PermissionError("user is not approved for APIYI Canary")
        with self.identity_lock:
            with self.platform.database.read() as connection:
                row = connection.execute(
                    """SELECT core_user_id,core_api_key_id FROM sub2api_user_authority
                    WHERE external_user_reference=?""",
                    (external_user,),
                ).fetchone()
            if row:
                user_id = int(row["core_user_id"])
                api_key_id = int(row["core_api_key_id"])
                with self.platform.database.transaction() as connection:
                    connection.execute(
                        """INSERT OR IGNORE INTO sub2api_identity_map(
                        external_user_reference,external_key_reference,
                        core_user_id,core_api_key_id) VALUES (?,?,?,?)""",
                        (external_user, external_key, user_id, api_key_id),
                    )
                return user_id, api_key_id

            password = secrets.token_urlsafe(32)
            user_id = self.platform.register_user(
                f"sub2api-{external_user}@isolated.invalid", password
            )
            initial_credit = int(os.getenv("AI16T_INITIAL_CREDIT_MICRO", "200000"))
            if self.provider_mode == "apiyi":
                # Canary balances are explicitly non-purchasable test grants. The
                # Ledger remains authoritative and the callback is idempotent.
                self.platform.ledger.test_credit(
                    user_id, initial_credit, f"apiyi-canary-credit-{external_user}"
                )
            else:
                self.platform.add_test_credit(
                    user_id, initial_credit, f"sub2api-bootstrap-{external_user}"
                )
            api_key_id, _discarded_raw = self.platform.create_api_key(user_id)
            with self.platform.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO sub2api_user_authority(
                    external_user_reference,core_user_id,core_api_key_id)
                    VALUES (?,?,?)""",
                    (external_user, user_id, api_key_id),
                )
                connection.execute(
                    """INSERT INTO sub2api_identity_map(
                    external_user_reference,external_key_reference,core_user_id,core_api_key_id)
                    VALUES (?,?,?,?)""",
                    (external_user, external_key, user_id, api_key_id),
                )
            return user_id, api_key_id

    def _request_replay(self, request_id: str) -> bool:
        with self.platform.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO bridge_requests(request_id) VALUES (?)", (request_id,)
            )
            return cursor.rowcount == 0

    @staticmethod
    def _request_payload(payload_b64: str) -> dict[str, Any]:
        raw = base64.b64decode(payload_b64, validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("request payload must be an object")
        endpoint = payload.get("_ai16t_endpoint", "chat.completions")
        if endpoint == "chat.completions":
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("messages are required")
        elif endpoint == "responses":
            if payload.get("input") in (None, "", []):
                raise ValueError("responses input is required")
        else:
            raise ValueError("unsupported endpoint")
        if type(payload.get("stream", False)) is not bool:
            raise ValueError("stream must be a boolean")
        return payload

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        (
            user_id,
            api_key_id,
            idempotency_key,
            model,
            request_payload,
            failure_plan,
            authoritative_request_id,
            prompt,
            max_output,
        ) = self._prepare_execution(payload)
        gate = (
            self.provider_spend_gate.admit(authoritative_request_id, user_id)
            if self.provider_spend_gate is not None
            else nullcontext()
        )
        with gate:
            replay = self._request_replay(authoritative_request_id)
            if request_payload.get("stream", False):
                chunks = list(
                    self.platform.process_stream_request(
                        user_id=user_id,
                        api_key_id=api_key_id,
                        provider=self.ledger_provider,
                        model=model,
                        prompt=prompt,
                        request_id=authoritative_request_id,
                        idempotency_key=idempotency_key,
                        failure_plan=failure_plan,
                        max_input_tokens=MAX_CANARY_INPUT_TOKENS,
                        max_output_tokens=max_output,
                    )
                )
                settlement = self.platform._existing_settlement(authoritative_request_id)
                response = "".join(chunk.content for chunk in chunks).encode()
                return self._result(settlement, replay, response, "text/event-stream")
            settlement = self.platform.process_request(
                user_id=user_id,
                api_key_id=api_key_id,
                provider=self.ledger_provider,
                model=model,
                prompt=prompt,
                request_id=authoritative_request_id,
                idempotency_key=idempotency_key,
                failure_plan=failure_plan,
                max_input_tokens=MAX_CANARY_INPUT_TOKENS,
                max_output_tokens=max_output,
            )
            return self._result(settlement, replay)

    def _prepare_execution(self, payload: dict[str, Any]) -> tuple[Any, ...]:
        user_id, api_key_id = self._identity(
            str(payload.get("UserReference", "")), str(payload.get("KeyReference", ""))
        )
        idempotency_key = str(payload.get("IdempotencyKey", ""))
        request_hash = str(payload.get("RequestHash", ""))
        model = str(payload.get("Model", ""))
        request_payload = self._request_payload(str(payload.get("Payload", "")))
        if (
            not idempotency_key
            or not request_hash
            or model not in self.allowed_models
            or request_payload.get("model") != model
        ):
            raise ValueError("invalid authority request")
        failure_plan = payload.get("FailurePlan") or None
        if self.provider_mode == "apiyi" and failure_plan:
            raise PermissionError("test hooks are disabled for APIYI")
        authoritative_request_id = "sub2_" + hashlib.sha256(
            f"{user_id}\0{idempotency_key}".encode()
        ).hexdigest()[:40]
        prompt = json.dumps(request_payload, separators=(",", ":"), ensure_ascii=False)
        enforce_canary_input_ceiling(prompt)
        max_output = request_payload.get(
            "max_output_tokens",
            request_payload.get("max_completion_tokens", request_payload.get("max_tokens", 256)),
        )
        if (
            not isinstance(max_output, int)
            or not 1 <= max_output <= MAX_CANARY_OUTPUT_TOKENS
        ):
            raise ValueError("max output tokens are invalid")
        return (
            user_id,
            api_key_id,
            idempotency_key,
            model,
            request_payload,
            failure_plan,
            authoritative_request_id,
            prompt,
            max_output,
        )

    def execute_stream(self, payload: dict[str, Any]) -> StreamBudgetSession:
        prepared = self._prepare_execution(payload)
        if not prepared[4].get("stream", False):
            raise ValueError("stream endpoint requires stream=true")
        # Admission is intentionally eager: a budget rejection must happen
        # before Handler._stream sends the SSE 200 response headers.
        gate = (
            self.provider_spend_gate.admit(prepared[6], prepared[0])
            if self.provider_spend_gate is not None
            else nullcontext()
        )
        return StreamBudgetSession(self._execute_stream(prepared, gate), gate)

    def _execute_stream(
        self, prepared: tuple[Any, ...], gate: Any
    ) -> Iterator[tuple[str, Any]]:
        (
            user_id,
            api_key_id,
            idempotency_key,
            model,
            _request_payload,
            failure_plan,
            authoritative_request_id,
            prompt,
            max_output,
        ) = prepared
        with gate:
            replay = self._request_replay(authoritative_request_id)
            iterator = self.platform.process_stream_request(
                user_id=user_id,
                api_key_id=api_key_id,
                provider=self.ledger_provider,
                model=model,
                prompt=prompt,
                request_id=authoritative_request_id,
                idempotency_key=idempotency_key,
                failure_plan=failure_plan,
                max_input_tokens=MAX_CANARY_INPUT_TOKENS,
                max_output_tokens=max_output,
            )
            try:
                for chunk in iterator:
                    yield (
                        "terminal" if chunk.finish_reason else "chunk",
                        chunk.content.encode("utf-8"),
                    )
            finally:
                iterator.close()
            settlement = self.platform._existing_settlement(authoritative_request_id)
            yield "settlement", self._result(settlement, replay, include_response=False)

    def _result(
        self,
        settlement: Any,
        replay: bool,
        response_override: bytes | None = None,
        content_type: str = "application/json",
        include_response: bool = True,
    ) -> dict[str, Any]:
        with self.platform.database.read() as connection:
            ledger = connection.execute(
                """SELECT transaction_id FROM ledger_entries WHERE request_id=?
                ORDER BY id DESC LIMIT 1""",
                (settlement.request_id,),
            ).fetchone()
            refund_row = connection.execute(
                "SELECT COALESCE(SUM(amount_micro),0) value FROM refunds WHERE request_id=?",
                (settlement.request_id,),
            ).fetchone()
        refund = int(refund_row["value"]) if refund_row else 0
        revenue = int(settlement.revenue_micro)
        response = b""
        if include_response:
            response = response_override or decode_raw_response(settlement.content)
        if include_response and response is None:
            response = json.dumps(
                {
                    "id": settlement.request_id,
                    "object": "chat.completion",
                    "model": "ai16t-mock",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": settlement.content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": settlement.usage.input_tokens,
                        "completion_tokens": settlement.usage.output_tokens,
                        "total_tokens": settlement.usage.total_tokens,
                    },
                },
                separators=(",", ":"),
            ).encode()
        settled = settlement.status in ("COMPLETED", "PARTIAL_SETTLED")
        return {
            "AuthoritativeRequestID": settlement.request_id,
            "LedgerReference": str(ledger["transaction_id"]) if ledger else "",
            "Status": "SETTLED" if settled else "FAILED_RELEASED",
            "Response": base64.b64encode(response).decode() if settled else "",
            "ContentType": content_type,
            "InputTokens": settlement.usage.input_tokens,
            "OutputTokens": settlement.usage.output_tokens,
            "CustomerChargeMicro": revenue,
            "ProviderCostMicro": settlement.cost_micro,
            "BalanceAfterMicro": settlement.balance_micro,
            "RefundMicro": refund,
            "NetRevenueMicro": revenue - refund,
            "Replay": replay,
        }

    def ready(self) -> None:
        if self.secret_resolver is not None:
            self.secret_resolver.ready()
            self.secret_resolver.resolve(self.secret_reference)

    def projection(self, external_user: str) -> dict[str, Any]:
        with self.platform.database.read() as connection:
            mapping = connection.execute(
                """SELECT core_user_id FROM sub2api_identity_map
                WHERE external_user_reference=? LIMIT 1""",
                (external_user,),
            ).fetchone()
            if not mapping:
                raise ValueError("identity not found")
            user_id = int(mapping["core_user_id"])
            request = connection.execute(
                "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
            ).fetchone()
            if not request:
                raise ValueError("request not found")
            balance = connection.execute(
                "SELECT available_micro FROM balances WHERE user_id=?", (user_id,)
            ).fetchone()
            ledger = connection.execute(
                """SELECT transaction_id FROM ledger_entries WHERE request_id=?
                ORDER BY id DESC LIMIT 1""",
                (request["request_id"],),
            ).fetchone()
            refund_row = connection.execute(
                "SELECT COALESCE(SUM(amount_micro),0) value FROM refunds WHERE request_id=?",
                (request["request_id"],),
            ).fetchone()
        refunded_micro = int(refund_row["value"]) if refund_row else 0
        return {
            "AuthoritativeRequestID": str(request["request_id"]),
            "IdempotencyReference": hashlib.sha256(
                str(request["idempotency_key"]).encode()
            ).hexdigest(),
            "LedgerReference": str(ledger["transaction_id"]),
            "UserReference": external_user,
            "KeyReference": "ledger-projection",
            "Model": str(request["model"]),
            "Status": (
                "SETTLED"
                if request["status"] in ("COMPLETED", "PARTIAL_SETTLED", "REFUNDED")
                else "FAILED_RELEASED"
            ),
            "InputTokens": int(request["input_tokens"]),
            "OutputTokens": int(request["output_tokens"]),
            "CustomerChargeMicro": int(request["customer_total_charge"]),
            "ProviderCostMicro": int(request["supplier_total_cost"]),
            "BalanceAfterMicro": int(balance["available_micro"]),
            "RefundMicro": refunded_micro,
            "NetRevenueMicro": int(request["customer_total_charge"])
            - refunded_micro,
            "Replay": True,
        }

    def refund(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("authoritative_request_id", ""))
        refund_id = str(payload.get("refund_id", ""))
        amount = payload.get("amount_micro")
        self.platform.ledger.refund(
            request_id, refund_id, None if amount is None else int(amount)
        )
        with self.platform.database.read() as connection:
            row = connection.execute(
                """SELECT sim.external_user_reference FROM requests r
                JOIN sub2api_identity_map sim ON sim.core_user_id=r.user_id
                WHERE r.request_id=? LIMIT 1""",
                (request_id,),
            ).fetchone()
        if not row:
            raise ValueError("refund projection identity not found")
        return self.projection(str(row["external_user_reference"]))


AUTHORITY = HybridAuthority()


class Handler(BaseHTTPRequestHandler):
    server_version = "AI16TCoreBridge/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string: str, *args: Any) -> None:
        del format_string, args

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 0 or length > MAX_BODY:
            raise ValueError("request body exceeds limit")
        return self.rfile.read(length)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _write_stream_chunk(self, payload: bytes) -> None:
        self.wfile.write(f"{len(payload):x}\r\n".encode("ascii"))
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream(self, iterator: StreamBudgetSession) -> None:
        # The session owns the eager budget reservation before headers. Keeping
        # header writes inside its context guarantees a pre-dispatch disconnect
        # cancels only this owner-token reservation and cannot lock the Canary.
        with iterator:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-store, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                for event, value in iterator:
                    if event in ("chunk", "terminal"):
                        data = json.dumps(
                            {"frame": base64.b64encode(value).decode("ascii")},
                            separators=(",", ":"),
                        )
                    else:
                        data = json.dumps(value, separators=(",", ":"))
                    encoded = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
                    self._write_stream_chunk(encoded)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception:
                # Headers may already be committed. Closing is safer than a
                # second JSON response; Ledger recovery remains fail-closed.
                pass
            finally:
                self.close_connection = True

    def _dispatch(self) -> None:
        try:
            body = self._body()
            if self.command == "GET" and self.path == "/health":
                self._write(200, {"status": "ok"})
                return
            if not AUTHORITY.authorize(self.command, self.path, body, self.headers):
                self._write(401, {"error": "invalid service signature"})
                return
            parsed = urlsplit(self.path)
            if self.command == "GET" and parsed.path == "/internal/v1/health":
                AUTHORITY.ready()
                self._write(200, {"status": "ok", "authority": "LEDGER_WINS"})
                return
            if self.command == "GET" and parsed.path == "/internal/v1/projection":
                user = parse_qs(parsed.query).get("user_reference", [""])[0]
                self._write(200, AUTHORITY.projection(user))
                return
            payload = json.loads(body or b"{}")
            if self.command == "POST" and parsed.path == "/internal/v1/execute-stream":
                self._stream(AUTHORITY.execute_stream(payload))
                return
            if self.command == "POST" and parsed.path == "/internal/v1/execute":
                self._write(200, AUTHORITY.execute(payload))
                return
            if self.command == "POST" and parsed.path == "/internal/v1/refund":
                self._write(200, AUTHORITY.refund(payload))
                return
            self._write(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self._write(400, {"error": type(error).__name__})
        except CanaryBudgetExceeded:
            self._write(429, {"error": "CANARY_BUDGET_LIMIT"})
        except PermissionError as error:
            self._write(403, {"error": type(error).__name__})
        except BaseException as error:
            self._write(503, {"error": type(error).__name__})

    do_GET = _dispatch
    do_POST = _dispatch


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8787), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
