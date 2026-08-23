from __future__ import annotations

import concurrent.futures
import json
import os
import secrets
import stat
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_URL = "http://127.0.0.1:18080"
RUNTIME = Path(__file__).resolve().parent / ".runtime"
COMPOSE = Path(__file__).resolve().parent / "compose.sandbox.yml"
ENV_FILE = RUNTIME / "hybrid.env"


def load_env() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


@dataclass
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes
    timed_out: bool = False

    def json(self) -> dict[str, Any]:
        return json.loads(self.body or b"{}")


def request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    bearer: str = "",
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> HTTPResult:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request_headers = {"Content-Type": "application/json"}
    if bearer:
        request_headers["Authorization"] = "Bearer " + bearer
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        BASE_URL + path, data=body, method=method, headers=request_headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return HTTPResult(
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read(2 * 1024 * 1024),
            )
    except urllib.error.HTTPError as error:
        return HTTPResult(
            error.code,
            {key.lower(): value for key, value in error.headers.items()},
            error.read(2 * 1024 * 1024),
        )
    except (TimeoutError, urllib.error.URLError):
        return HTTPResult(0, {}, b"", timed_out=True)


class Harness:
    def __init__(self) -> None:
        self.env = load_env()
        self.run_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        self.results: list[dict[str, Any]] = []
        login = request(
            "POST",
            "/api/v1/auth/login",
            payload={
                "email": self.env["ADMIN_EMAIL"],
                "password": self.env["ADMIN_PASSWORD"],
            },
        )
        if login.status != 200:
            raise RuntimeError("admin login failed")
        self.admin_token = str(login.json()["data"]["access_token"])

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        outcome = "PASS" if condition else "FAIL"
        print(f"{len(self.results) + 1:02d} {name}: {outcome}")
        self.results.append({"name": name, "result": outcome, "detail": detail})

    def new_identity(self, label: str) -> tuple[str, str, str]:
        email = f"mac1-e2e-{label}-{self.run_id}@example.invalid"
        password = secrets.token_urlsafe(24)
        hashed = subprocess.run(
            ["htpasswd", "-nBC", "10", "mac1-e2e"],
            input=password + "\n" + password + "\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().split(":", 1)[1]
        created = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_FILE),
                "-f",
                str(COMPOSE),
                "exec",
                "-T",
                "postgres",
                "psql",
                "-XAt",
                "-v",
                f"fixture_email={email}",
                "-v",
                f"fixture_hash={hashed}",
                "-U",
                "ai16t_sub2api",
                "-d",
                "ai16t_sub2api",
            ],
            input=(
                "INSERT INTO users(email,password_hash,role,balance,concurrency,status) "
                "VALUES (:'fixture_email',:'fixture_hash','user',0,20,'active') RETURNING id;\n"
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        user_id = next(
            (line for line in created.stdout.splitlines() if line.strip().isdigit()), ""
        )
        if not user_id:
            raise RuntimeError("isolated PostgreSQL fixture insert failed")
        login = request(
            "POST", "/api/v1/auth/login", payload={"email": email, "password": password}
        )
        if login.status != 200:
            raise RuntimeError("user login failed")
        user_token = str(login.json()["data"]["access_token"])
        key = request(
            "POST",
            "/api/v1/keys",
            bearer=user_token,
            headers={"Idempotency-Key": f"create-key-{label}-{self.run_id}"},
            payload={"name": f"Mac1 E2E {label}"},
        )
        if key.status != 200:
            raise RuntimeError(f"create API key failed: HTTP {key.status}")
        return user_id, str(key.json()["data"]["key"]), user_token

    @staticmethod
    def chat_payload(content: str) -> dict[str, Any]:
        return {
            "model": "ai16t-mock",
            "messages": [{"role": "user", "content": content}],
        }

    def chat(
        self,
        api_key: str,
        idempotency: str,
        content: str,
        *,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> HTTPResult:
        headers = {"Idempotency-Key": idempotency}
        if extra_headers:
            headers.update(extra_headers)
        return request(
            "POST",
            "/v1/ai16t/chat/completions",
            bearer=api_key,
            headers=headers,
            payload=self.chat_payload(content),
            timeout=timeout,
        )

    def evidence(self, user_id: str) -> dict[str, Any]:
        output = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_FILE),
                "-f",
                str(COMPOSE),
                "exec",
                "-T",
                "commercial-core",
                "python",
                "/evidence/core_evidence.py",
                "user",
                user_id,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(output.stdout)

    def run(self) -> None:
        ready = request("GET", "/ready")
        self.check(
            "READY_REAL_HTTP",
            ready.status == 200 and ready.json().get("ledger_authority") == "AI16T_COMMERCIAL_CORE",
        )
        missing = request(
            "POST", "/v1/ai16t/chat/completions", payload=self.chat_payload("missing")
        )
        self.check("MISSING_KEY_REJECTED", missing.status == 401)

        idem_user, idem_key, _ = self.new_identity("idem")
        self.check("REAL_POSTGRES_AUTH", bool(idem_user) and bool(idem_key))
        invalid = self.chat("not-a-real-key", "invalid-key", "invalid")
        self.check("REAL_API_KEY_AUTH", invalid.status == 401)
        first = self.chat(idem_key, "idem-shared", "same payload")
        first_json = first.json() if first.status == 200 else {}
        self.check(
            "FULL_HYBRID_SUCCESS",
            first.status == 200
            and first_json.get("usage", {}).get("total_tokens") == 1000
            and first_json.get("choices", [{}])[0].get("message", {}).get("content", "").startswith("mock response"),
        )
        self.check(
            "LEDGER_AUTHORITY_HEADER",
            first.headers.get("x-ai16t-ledger-authority") == "AI16T_COMMERCIAL_CORE",
        )
        projection = request("GET", "/v1/ai16t/projection", bearer=idem_key)
        self.check(
            "PROJECTION_LEDGER_WINS",
            projection.status == 200
            and projection.json().get("authority") == "LEDGER_WINS"
            and projection.json().get("projection", {}).get("AuthoritativeRequestID")
            == first_json.get("id"),
        )
        replay = self.chat(idem_key, "idem-shared", "same payload")
        self.check(
            "IDEMPOTENT_REPLAY_SAME_REQUEST",
            replay.status == 200
            and replay.json().get("id") == first_json.get("id")
            and replay.headers.get("x-ai16t-idempotent-replay") == "true",
        )
        idem_evidence = self.evidence(idem_user)
        self.check(
            "IDEMPOTENT_SINGLE_REQUEST",
            idem_evidence["requests"] == 1
            and idem_evidence["ledger_settlements"] == 1
            and idem_evidence["duplicate_settlements"] == 0,
        )
        conflict = self.chat(idem_key, "idem-shared", "changed payload")
        self.check("IDEMPOTENCY_CONFLICT_REJECTED", conflict.status != 200)

        concurrent_user, concurrent_key, _ = self.new_identity("concurrent")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(
                    self.chat,
                    concurrent_key,
                    f"concurrent-{index}",
                    f"payload-{index}",
                )
                for index in range(10)
            ]
            concurrent_results = [future.result() for future in futures]
        success_count = sum(item.status == 200 for item in concurrent_results)
        self.check(
            "TEN_CONCURRENT_COMPLETED",
            len(concurrent_results) == 10
            and success_count > 0
            and all(item.status in (200, 503) for item in concurrent_results),
        )
        concurrent_evidence = self.evidence(concurrent_user)
        self.check(
            "CONCURRENT_NO_NEGATIVE",
            not concurrent_evidence["negative_balance"]
            and concurrent_evidence["available_micro"] >= 0,
        )
        self.check(
            "CONCURRENT_NO_DUP_SETTLE",
            concurrent_evidence["duplicate_settlements"] == 0
            and concurrent_evidence["duplicate_idempotency"] == 0
            and concurrent_evidence["ledger_settlements"] == success_count,
        )
        self.check(
            "CONCURRENT_RESERVES_RELEASED",
            concurrent_evidence["reserved_micro"] == 0,
        )

        timeout_user, timeout_key, _ = self.new_identity("timeout")
        fallback = self.chat(
            timeout_key,
            "timeout-fallback",
            "timeout fallback",
            extra_headers={
                "X-AI16T-Test-Failure-Plan": json.dumps({"openai-a": "timeout"})
            },
        )
        self.check("TIMEOUT_FALLBACK_SUCCESS", fallback.status == 200)
        timeout_evidence = self.evidence(timeout_user)
        self.check(
            "TIMEOUT_SINGLE_SETTLEMENT",
            timeout_evidence["ledger_settlements"] == 1
            and timeout_evidence["provider_attempts"] == 2,
        )
        delayed = self.chat(
            timeout_key,
            "client-timeout",
            "client timeout",
            extra_headers={"X-AI16T-Test-Client-Delay-Ms": "500"},
            timeout=0.1,
        )
        replay_delayed = self.chat(timeout_key, "client-timeout", "client timeout")
        self.check(
            "CLIENT_TIMEOUT_REPLAY_SAFE",
            delayed.timed_out and replay_delayed.status == 200,
        )
        timeout_evidence = self.evidence(timeout_user)
        self.check(
            "CLIENT_TIMEOUT_SINGLE_SETTLEMENT",
            timeout_evidence["ledger_settlements"] == 2
            and timeout_evidence["duplicate_settlements"] == 0,
        )

        failed_user, failed_key, _ = self.new_identity("failed")
        failed = self.chat(
            failed_key,
            "all-failed",
            "all failed",
            extra_headers={
                "X-AI16T-Test-Failure-Plan": json.dumps(
                    {"openai-a": "5xx", "openai-b": "5xx"}
                )
            },
        )
        failed_evidence = self.evidence(failed_user)
        self.check(
            "ALL_PROVIDER_FAILURE_ZERO_CHARGE",
            failed.status == 502
            and failed_evidence["gross_charge_micro"] == 0
            and failed_evidence["ledger_settlements"] == 0,
        )
        self.check(
            "FAILED_REQUEST_RELEASES_RESERVE",
            failed_evidence["failed_released_requests"] == 1
            and failed_evidence["reserved_micro"] == 0,
        )

        insufficient_user, insufficient_key, _ = self.new_identity("insufficient")
        insufficient_result = HTTPResult(200, {}, b"")
        for index in range(20):
            insufficient_result = self.chat(
                insufficient_key, f"drain-{index}", f"drain-{index}"
            )
            if insufficient_result.status != 200:
                break
        insufficient_evidence = self.evidence(insufficient_user)
        self.check(
            "INSUFFICIENT_BALANCE_FAIL_CLOSED",
            insufficient_result.status == 503
            and insufficient_evidence["available_micro"] >= 0
            and insufficient_evidence["reserved_micro"] == 0,
        )

        secret_paths = [
            RUNTIME / "core_signing_key",
            RUNTIME / "fingerprint_key",
            RUNTIME / "mock_provider_key",
            ENV_FILE,
        ]
        self.check(
            "SECRET_FILE_MODE_0600",
            all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in secret_paths),
        )
        provider_secret = (RUNTIME / "mock_provider_key").read_text(encoding="utf-8").strip()
        git_scan = subprocess.run(
            ["git", "grep", "-F", "--", provider_secret], capture_output=True, text=True
        )
        core_scan_output = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_FILE),
                "-f",
                str(COMPOSE),
                "exec",
                "-T",
                "commercial-core",
                "python",
                "/evidence/core_evidence.py",
                "secret-scan",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        core_scan = json.loads(core_scan_output.stdout)
        logs = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_FILE),
                "-f",
                str(COMPOSE),
                "logs",
                "--no-color",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.check(
            "SECRET_NOT_IN_GIT_DB_LOGS",
            git_scan.returncode == 1
            and not core_scan["provider_secret_persisted_in_core_db"]
            and provider_secret not in logs,
        )

        drift_user, drift_key, _ = self.new_identity("drift")
        drifted = self.chat(
            drift_key,
            "drift-first",
            "drift",
            extra_headers={"X-AI16T-Test-Projection-Failure": "1"},
        )
        blocked = self.chat(drift_key, "drift-blocked", "blocked")
        self.check(
            "PROJECTION_DRIFT_BLOCKS",
            drifted.status == 200
            and drifted.headers.get("x-ai16t-projection-drift") == "PROJECTION_DRIFT"
            and blocked.status == 409,
        )
        reconciled = request(
            "POST",
            "/api/v1/admin/ai16t/reconcile",
            bearer=self.admin_token,
            payload={"user_reference": drift_user},
        )
        after_reconcile = self.chat(drift_key, "drift-after", "after")
        self.check(
            "LEDGER_RECONCILIATION_CLEARS",
            reconciled.status == 200
            and reconciled.json().get("authority") == "LEDGER_WINS"
            and after_reconcile.status == 200,
        )
        refunded = request(
            "POST",
            "/api/v1/admin/ai16t/refund",
            bearer=self.admin_token,
            payload={
                "authoritative_request_id": drifted.json().get("id"),
                "refund_id": f"refund-{self.run_id}",
            },
        )
        drift_evidence = self.evidence(drift_user)
        self.check(
            "REFUND_LEDGER_PROJECTION",
            refunded.status == 200
            and drift_evidence["ledger_refunds"] == 1
            and drift_evidence["refund_micro"] > 0
            and drift_evidence["net_revenue_micro"]
            == drift_evidence["gross_charge_micro"] - drift_evidence["refund_micro"],
        )

        if len(self.results) != 26:
            raise RuntimeError(f"expected 26 checks, got {len(self.results)}")
        failed_checks = [item for item in self.results if item["result"] != "PASS"]
        evidence = {
            "run_id": self.run_id,
            "executed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "result": "PASS" if not failed_checks else "FAIL",
            "passed": 26 - len(failed_checks),
            "total": 26,
            "checks": self.results,
        }
        output = RUNTIME / "dynamic_e2e_result.json"
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output.chmod(0o600)
        print(f"DYNAMIC_E2E={evidence['passed']}/26 {evidence['result']}")
        if failed_checks:
            raise SystemExit(1)


if __name__ == "__main__":
    Harness().run()
