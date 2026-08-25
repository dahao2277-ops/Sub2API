from __future__ import annotations

import json
import os
import stat
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_BODY = 1024 * 1024


def read_secret(path_value: str) -> str:
    path = Path(path_value)
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError("secret reference is invalid")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise RuntimeError("secret reference mode is invalid")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError("secret reference is too short")
    return value


API_KEY = read_secret(os.environ["AI16T_MOCK_PROVIDER_KEY_FILE"])


class Handler(BaseHTTPRequestHandler):
    server_version = "AI16TMockProvider/1"

    def log_message(self, format_string: str, *args: Any) -> None:
        del format_string, args

    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.write_json(200, {"status": "ok"})
        else:
            self.write_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/invoke":
            self.write_json(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != "Bearer " + API_KEY:
            self.write_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                raise ValueError("invalid length")
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.write_json(400, {"error": "invalid request"})
            return
        failure = payload.get("failure")
        started = time.monotonic()
        if failure in ("slow", "slow_lease"):
            default_delay = "0.15" if failure == "slow" else "0.35"
            delay = float(
                os.getenv("AI16T_MOCK_SLOW_LEASE_DELAY_SECONDS", default_delay)
            )
            if not 0.05 <= delay <= 15:
                self.write_json(400, {"error": "invalid mock delay"})
                return
            time.sleep(delay)
        if failure == "timeout":
            time.sleep(2.0)
        if failure in {
            "timeout",
            "429",
            "5xx",
            "health_failed",
            "balance_insufficient",
            "model_unavailable",
            "provider_disconnect",
            "stream_timeout",
            "metered_5xx",
            "client_disconnect",
            "missing_usage",
            "invalid_usage",
        }:
            partial = None
            source = "confirmed_none"
            retryable = failure != "client_disconnect"
            if failure == "metered_5xx":
                partial = {"input_tokens": 100, "output_tokens": 50, "cached_tokens": 0}
                source = "provider_reported"
            elif failure == "missing_usage":
                source = "missing"
                retryable = False
            elif failure == "invalid_usage":
                partial = {"input_tokens": -1, "output_tokens": 0, "cached_tokens": 0}
                source = "provider_reported"
                retryable = False
            self.write_json(
                429 if failure == "429" else 503,
                {
                    "code": failure,
                    "retryable": retryable,
                    "usage_source": source,
                    "partial_usage": partial,
                },
            )
            return
        prompt = str(payload.get("prompt", ""))
        self.write_json(
            200,
            {
                "content": f"mock response ({len(prompt)} chars)",
                "usage": {"input_tokens": 400, "output_tokens": 600, "cached_tokens": 0},
                "latency_ms": max(1, int((time.monotonic() - started) * 1000)),
            },
        )


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 9090), Handler).serve_forever()


if __name__ == "__main__":
    main()
