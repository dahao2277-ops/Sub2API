from __future__ import annotations

import os
from typing import Any

from core_bridge import AUTHORITY, Handler as StableHandler, ThreadingHTTPServer


def _version() -> dict[str, Any]:
    with AUTHORITY.platform.database.read() as connection:
        row = connection.execute(
            "SELECT MAX(version) version FROM schema_version"
        ).fetchone()
    return {
        "status": "ok",
        "slot": "blue",
        "core_commit": os.environ.get("AI16T_CORE_RELEASE_COMMIT", "unknown"),
        "bridge_commit": os.environ.get("AI16T_BRIDGE_RELEASE_COMMIT", "unknown"),
        "schema_version": int(row["version"] or 0) if row else 0,
        "financial_runtime": "unmodified_stable_base",
    }


class Handler(StableHandler):
    """Operational endpoints around the unmodified stable financial runtime."""

    def _dispatch(self) -> None:
        if self.command == "GET" and self.path == "/ready":
            try:
                AUTHORITY.ready()
                self._write(200, {"status": "ok", "authority": "LEDGER_WINS"})
            except Exception as error:  # noqa: BLE001 - return only the type.
                self._write(503, {"error": type(error).__name__})
            return
        if self.command == "GET" and self.path == "/version":
            self._write(200, _version())
            return
        if self.command == "POST" and self.path == "/internal/v1/recover-incomplete":
            try:
                body = self._body()
                if not AUTHORITY.authorize(self.command, self.path, body, self.headers):
                    self._write(401, {"error": "invalid service signature"})
                    return
                if AUTHORITY.provider_mode != "mock" or not AUTHORITY.settings.test_mode:
                    raise PermissionError(
                        "recovery endpoint is restricted to isolated Mock mode"
                    )
                recovered = AUTHORITY.platform.recover_incomplete_requests()
                self._write(
                    200,
                    {"status": "ok", "recovered_request_ids": recovered},
                )
            except PermissionError as error:
                self._write(403, {"error": type(error).__name__})
            except Exception as error:  # noqa: BLE001 - return only the type.
                self._write(503, {"error": type(error).__name__})
            return
        super()._dispatch()

    do_GET = _dispatch
    do_POST = _dispatch


def main() -> None:
    provider = AUTHORITY.platform.gateway.provider
    if AUTHORITY.provider_mode == "mock" and hasattr(provider, "timeout_seconds"):
        provider.timeout_seconds = float(
            os.getenv("AI16T_MOCK_PROVIDER_TIMEOUT_SECONDS", "10")
        )
    port = int(os.getenv("TP_BIND_PORT", "8787"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
