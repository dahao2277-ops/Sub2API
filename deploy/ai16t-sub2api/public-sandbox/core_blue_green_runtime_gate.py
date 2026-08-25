from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


DB_SNAPSHOT = r"""
import json, sqlite3
c = sqlite3.connect('file:/data/core-blue-green-gate.sqlite3?mode=ro', uri=True)
c.row_factory = sqlite3.Row
def scalar(sql, args=()):
    return int(c.execute(sql, args).fetchone()[0])
payload = {
    'available_micro': scalar('SELECT COALESCE(SUM(available_micro),0) FROM balances'),
    'reserved_micro': scalar('SELECT COALESCE(SUM(reserved_micro),0) FROM balances'),
    'request_count': scalar('SELECT COUNT(*) FROM requests'),
    'active_request_count': scalar("SELECT COUNT(*) FROM requests WHERE status IN ('PREAUTHORIZED','PROCESSING')"),
    'ledger_count': scalar('SELECT COUNT(*) FROM ledger_entries'),
    'audit_count': scalar('SELECT COUNT(*) FROM audit_log'),
    'duplicate_transaction_ids': scalar('SELECT COUNT(*) FROM (SELECT transaction_id FROM ledger_entries GROUP BY transaction_id HAVING COUNT(*)>1)'),
    'duplicate_settlements': scalar("SELECT COUNT(*) FROM (SELECT request_id FROM ledger_entries WHERE transaction_type='DEBIT_SETTLEMENT' GROUP BY request_id HAVING COUNT(*)>1)"),
    'duplicate_request_idempotency': scalar('SELECT COUNT(*) FROM (SELECT api_key_id,idempotency_key FROM requests GROUP BY api_key_id,idempotency_key HAVING COUNT(*)>1)'),
    'statuses': {str(row['status']): int(row['n']) for row in c.execute('SELECT status,COUNT(*) n FROM requests GROUP BY status')},
}
print(json.dumps(payload, sort_keys=True, separators=(',', ':')))
"""

REQUEST_ROW = r"""
import json, sqlite3, sys
c = sqlite3.connect('file:/data/core-blue-green-gate.sqlite3?mode=ro', uri=True)
c.row_factory = sqlite3.Row
row = c.execute('SELECT request_id,status,preauth_micro FROM requests WHERE idempotency_key=?', (sys.argv[1],)).fetchone()
if row is None:
    print('{}')
else:
    attempts = c.execute('SELECT COUNT(*) FROM provider_attempts WHERE request_id=?', (row['request_id'],)).fetchone()[0]
    settlements = c.execute("SELECT COUNT(*) FROM ledger_entries WHERE request_id=? AND transaction_type='DEBIT_SETTLEMENT'", (row['request_id'],)).fetchone()[0]
    print(json.dumps({'request_id': row['request_id'], 'status': row['status'], 'preauth_micro': row['preauth_micro'], 'attempt_count': attempts, 'settlement_count': settlements}, sort_keys=True, separators=(',', ':')))
"""

HTTP_CLIENT = r"""
import base64, json, sys, urllib.error, urllib.request
url, method, headers_b64, body_b64, timeout = sys.argv[1:]
headers = json.loads(base64.b64decode(headers_b64))
body = base64.b64decode(body_b64) if body_b64 else None
request = urllib.request.Request(url, data=body, method=method, headers=headers)
try:
    with urllib.request.urlopen(request, timeout=float(timeout)) as response:
        print(json.dumps({'status': response.status, 'body': base64.b64encode(response.read()).decode('ascii')}, separators=(',', ':')))
except urllib.error.HTTPError as error:
    print(json.dumps({'status': error.code, 'body': base64.b64encode(error.read()).decode('ascii')}, separators=(',', ':')))
"""


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError("invalid gate environment file")
        values[key] = value
    return values


class Gate:
    def __init__(self, compose_file: Path, env_file: Path, project: str) -> None:
        self.compose_file = compose_file
        self.env_file = env_file
        self.project = project
        self.values = _parse_env(env_file)
        self.signing_key = Path(self.values["CORE_GATE_SIGNING_KEY_FILE"]).read_bytes().strip()
        if len(self.signing_key) < 32:
            raise RuntimeError("gate signing key is too short")
        self.blue_url = "http://commercial-core-blue:8787"
        self.green_url = "http://commercial-core-green:8788"
        self.assertions: list[dict[str, Any]] = []

    def compose(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.compose_file),
            *arguments,
        ]
        return subprocess.run(
            command,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def assert_gate(self, name: str, condition: bool, evidence: Any) -> None:
        self.assertions.append({"name": name, "pass": bool(condition), "evidence": evidence})
        if not condition:
            raise AssertionError(name)

    def _http(
        self,
        base_url: str,
        path: str,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        encoded_headers = base64.b64encode(
            json.dumps(headers, sort_keys=True, separators=(",", ":")).encode()
        ).decode("ascii")
        encoded_body = base64.b64encode(body).decode("ascii") if body else ""
        result = self.compose(
            "exec",
            "-T",
            "gate-client",
            "python",
            "-c",
            HTTP_CLIENT,
            base_url + path,
            method,
            encoded_headers,
            encoded_body,
            str(timeout),
        )
        payload = json.loads(result.stdout.strip())
        return int(payload["status"]), base64.b64decode(payload["body"])

    def get_json(self, base_url: str, path: str) -> dict[str, Any]:
        status, body = self._http(base_url, path, "GET", {}, None, 5)
        if status != 200:
            raise RuntimeError("unexpected health status")
        return json.loads(body)

    def signed_json(
        self, base_url: str, path: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(24)
        digest = hashlib.sha256(body).hexdigest()
        canonical = f"POST\n{path}\n{timestamp}\n{nonce}\n{digest}".encode()
        signature = hmac.new(self.signing_key, canonical, hashlib.sha256).hexdigest()
        status, response_body = self._http(
            base_url,
            path,
            "POST",
            {
                "Content-Type": "application/json",
                "X-AI16T-Timestamp": timestamp,
                "X-AI16T-Nonce": nonce,
                "X-AI16T-Signature": signature,
            },
            body,
            20,
        )
        return status, json.loads(response_body or b"{}")

    def unsigned_json(self, base_url: str, path: str) -> int:
        status, _ = self._http(
            base_url,
            path,
            "POST",
            {"Content-Type": "application/json"},
            b"{}",
            5,
        )
        return status

    def execute(
        self,
        base_url: str,
        idempotency_key: str,
        *,
        failure: str | None = None,
    ) -> dict[str, Any]:
        request_payload = {
            "_ai16t_endpoint": "chat.completions",
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "core blue green gate"}],
            "max_tokens": 1024,
        }
        encoded = json.dumps(
            request_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        payload: dict[str, Any] = {
            "UserReference": "core-bg-gate-user",
            "KeyReference": "core-bg-gate-key",
            "IdempotencyKey": idempotency_key,
            "RequestHash": hashlib.sha256(encoded).hexdigest(),
            "Model": "gpt-4o-mini",
            "Payload": base64.b64encode(encoded).decode("ascii"),
        }
        if failure:
            payload["FailurePlan"] = {"openai-a": failure}
        status, result = self.signed_json(base_url, "/internal/v1/execute", payload)
        if status != 200:
            raise RuntimeError(f"Core execute returned HTTP {status}")
        return result

    def snapshot(self) -> dict[str, Any]:
        result = self.compose(
            "exec", "-T", "commercial-core-blue", "python", "-c", DB_SNAPSHOT
        )
        return json.loads(result.stdout.strip())

    def request_row(self, idempotency_key: str) -> dict[str, Any]:
        result = self.compose(
            "exec",
            "-T",
            "commercial-core-blue",
            "python",
            "-c",
            REQUEST_ROW,
            idempotency_key,
        )
        return json.loads(result.stdout.strip())

    def wait_for_status(self, idempotency_key: str, status: str) -> dict[str, Any]:
        deadline = time.monotonic() + 6
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.request_row(idempotency_key)
            if last.get("status") == status:
                return last
            time.sleep(0.05)
        raise AssertionError(
            f"request {idempotency_key} did not reach {status}: {last}"
        )

    def image_evidence(self, image: str) -> dict[str, Any]:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                "{{json .}}",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        details = json.loads(result.stdout)
        labels = details.get("Config", {}).get("Labels", {}) or {}
        return {
            "id": details.get("Id", ""),
            "revision": labels.get("org.opencontainers.image.revision", ""),
            "role": labels.get("ai16t.runtime.role", ""),
            "financial_code": labels.get("ai16t.runtime.financial-code", ""),
            "stable_base_digest": labels.get("ai16t.runtime.stable-base-digest", ""),
        }

    def run(self) -> dict[str, Any]:
        run_nonce = secrets.token_hex(6)
        ids = {
            "blue_normal": "bg-blue-normal-" + run_nonce,
            "blue_slow": "bg-blue-slow-" + run_nonce,
            "green_new": "bg-green-new-" + run_nonce,
            "cross": "bg-cross-" + run_nonce,
            "green_crash": "bg-green-crash-" + run_nonce,
            "blue_rollback": "bg-blue-rollback-" + run_nonce,
        }
        blue_image = self.image_evidence(self.values["CORE_GATE_BLUE_IMAGE"])
        green_image = self.image_evidence(self.values["CORE_GATE_GREEN_IMAGE"])
        self.assert_gate(
            "IMMUTABLE_IMAGES_DIFFER",
            bool(blue_image["id"] and green_image["id"])
            and blue_image["id"] != green_image["id"],
            {"blue": blue_image, "green": green_image},
        )

        self.compose(
            "up", "-d", "--no-deps", "--wait", "mock-provider", "gate-client"
        )
        self.compose("up", "-d", "--no-deps", "--wait", "commercial-core-blue")
        blue_health = self.get_json(self.blue_url, "/health")
        blue_ready = self.get_json(self.blue_url, "/ready")
        blue_version = self.get_json(self.blue_url, "/version")
        self.assert_gate(
            "BLUE_HEALTH_READY_VERSION",
            blue_health.get("status") == "ok"
            and blue_ready.get("status") == "ok"
            and blue_version.get("slot") == "blue",
            {"health": blue_health, "ready": blue_ready, "version": blue_version},
        )
        initial = self.snapshot()
        normal = self.execute(self.blue_url, ids["blue_normal"])
        self.assert_gate("BLUE_NORMAL_REQUEST", normal.get("Status") == "SETTLED", normal)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            blue_slow = executor.submit(
                self.execute,
                self.blue_url,
                ids["blue_slow"],
                failure="slow_lease",
            )
            slow_row = self.wait_for_status(ids["blue_slow"], "PROCESSING")
            slow_snapshot = self.snapshot()
            self.assert_gate(
                "BLUE_IN_FLIGHT_RESERVATION",
                slow_snapshot["reserved_micro"] > 0,
                {"request": slow_row, "snapshot": slow_snapshot},
            )
            self.compose(
                "up", "-d", "--no-deps", "--wait", "commercial-core-green"
            )
            green_health = self.get_json(self.green_url, "/health")
            green_ready = self.get_json(self.green_url, "/ready")
            green_version = self.get_json(self.green_url, "/version")
            self.assert_gate(
                "GREEN_HEALTH_READY_VERSION_WHILE_BLUE_IN_FLIGHT",
                not blue_slow.done()
                and green_health.get("status") == "ok"
                and green_ready.get("status") == "ok"
                and green_version.get("slot") == "green",
                {
                    "health": green_health,
                    "ready": green_ready,
                    "version": green_version,
                    "blue_in_flight": not blue_slow.done(),
                },
            )
            green_new = self.execute(self.green_url, ids["green_new"])
            self.assert_gate(
                "NEW_REQUEST_SWITCHED_TO_GREEN",
                green_new.get("Status") == "SETTLED",
                green_new,
            )
            blue_slow_result = blue_slow.result(timeout=25)
        self.assert_gate(
            "BLUE_IN_FLIGHT_COMPLETED",
            blue_slow_result.get("Status") == "SETTLED",
            blue_slow_result,
        )

        replay_before = self.snapshot()
        replay = self.execute(self.green_url, ids["blue_slow"])
        replay_after = self.snapshot()
        self.assert_gate(
            "GREEN_RETRY_REPLAYED_BLUE_IDEMPOTENCY",
            replay.get("AuthoritativeRequestID")
            == blue_slow_result.get("AuthoritativeRequestID")
            and replay.get("LedgerReference")
            == blue_slow_result.get("LedgerReference")
            and replay_before == replay_after,
            {"result": replay, "counts_before": replay_before, "counts_after": replay_after},
        )

        cross_before = self.snapshot()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            cross_blue = executor.submit(self.execute, self.blue_url, ids["cross"])
            cross_green = executor.submit(self.execute, self.green_url, ids["cross"])
            cross_results = [
                cross_blue.result(timeout=8),
                cross_green.result(timeout=8),
            ]
        cross_row = self.request_row(ids["cross"])
        cross_after = self.snapshot()
        self.assert_gate(
            "CROSS_INSTANCE_IDEMPOTENCY",
            cross_results[0].get("AuthoritativeRequestID")
            == cross_results[1].get("AuthoritativeRequestID")
            and cross_results[0].get("LedgerReference")
            == cross_results[1].get("LedgerReference")
            and cross_row.get("attempt_count") == 1
            and cross_row.get("settlement_count") == 1
            and cross_after["request_count"] == cross_before["request_count"] + 1,
            {"results": cross_results, "request": cross_row},
        )

        unauthorized_before = self.snapshot()
        unauthorized_statuses = [
            self.unsigned_json(self.blue_url, "/internal/v1/execute"),
            self.unsigned_json(self.green_url, "/internal/v1/execute"),
        ]
        unauthorized_after = self.snapshot()
        self.assert_gate(
            "SERVICE_AUTH_UNAUTHORIZED_REJECTED",
            unauthorized_statuses == [401, 401]
            and unauthorized_before == unauthorized_after,
            {"statuses": unauthorized_statuses},
        )

        pre_crash = self.snapshot()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            crash_future = executor.submit(
                self.execute,
                self.green_url,
                ids["green_crash"],
                failure="slow_lease",
            )
            crash_row = self.wait_for_status(ids["green_crash"], "PROCESSING")
            crash_active = self.snapshot()
            self.assert_gate(
                "GREEN_CRASH_HAS_ACTIVE_RESERVATION",
                crash_active["reserved_micro"] > 0,
                {"request": crash_row, "snapshot": crash_active},
            )
            self.compose("kill", "-s", "SIGKILL", "commercial-core-green")
            try:
                crash_future.result(timeout=5)
                crash_client_failed = False
            except Exception:  # noqa: BLE001 - a killed server must abort the client.
                crash_client_failed = True
        self.assert_gate("GREEN_PROCESS_KILLED_IN_FLIGHT", crash_client_failed, True)
        time.sleep(0.8)
        recover_status, recover = self.signed_json(
            self.blue_url, "/internal/v1/recover-incomplete", {}
        )
        recovered_row = self.request_row(ids["green_crash"])
        after_recovery = self.snapshot()
        self.assert_gate(
            "BLUE_RECOVERS_GREEN_IN_FLIGHT",
            recover_status == 200
            and recovered_row.get("request_id")
            in recover.get("recovered_request_ids", [])
            and recovered_row.get("status") == "RECONCILIATION_REQUIRED"
            and recovered_row.get("settlement_count") == 0
            and after_recovery["reserved_micro"] == 0
            and after_recovery["available_micro"] == pre_crash["available_micro"],
            {"recover": recover, "request": recovered_row, "snapshot": after_recovery},
        )

        rollback = self.execute(self.blue_url, ids["blue_rollback"])
        self.assert_gate(
            "NEW_REQUEST_ROLLED_BACK_TO_BLUE",
            rollback.get("Status") == "SETTLED",
            rollback,
        )
        self.compose("up", "-d", "--no-deps", "--wait", "commercial-core-green")
        restarted_green = self.get_json(self.green_url, "/version")
        self.assert_gate(
            "GREEN_RESTARTS_WITH_SHARED_SCHEMA",
            restarted_green.get("schema_version") == blue_version.get("schema_version"),
            restarted_green,
        )

        final = self.snapshot()
        self.assert_gate(
            "FINAL_LEDGER_INVARIANTS",
            final["request_count"] == initial["request_count"] + 6
            and final["active_request_count"] == 0
            and final["reserved_micro"] == 0
            and final["duplicate_transaction_ids"] == 0
            and final["duplicate_settlements"] == 0
            and final["duplicate_request_idempotency"] == 0
            and final["statuses"].get("COMPLETED", 0) == 5
            and final["statuses"].get("RECONCILIATION_REQUIRED", 0) == 1,
            final,
        )
        return {
            "gate_status": "PASS",
            "project": self.project,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "images": {"blue": blue_image, "green": green_image},
            "versions": {"blue": blue_version, "green": green_version},
            "initial_snapshot": initial,
            "final_snapshot": final,
            "assertions": self.assertions,
            "summary": {
                "CORE_BLUE_GREEN_SWITCH": "PASS",
                "CORE_GREEN_BLUE_ROLLBACK": "PASS",
                "CROSS_INSTANCE_IDEMPOTENCY": "PASS",
                "ACTIVE_RESERVATIONS": 0,
                "DUPLICATE_LEDGER_TX": 0,
                "APIYI_REQUESTS": 0,
                "REAL_COST_USD": "0.000000",
            },
        }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    gate = Gate(args.compose_file.resolve(), args.env_file.resolve(), args.project)
    result = gate.run()
    _atomic_write(args.output.resolve(), result)
    print(json.dumps(result["summary"], sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
