from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

from apiyi_provider import APIYIProvider
from apiyi_transport import SECRET_PATTERN
from secret_provider_client import UnixSecretResolver

REFERENCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{1,127}$")


def run_probe() -> dict[str, Any]:
    reference = os.environ.get("AI16T_PROVIDER_SECRET_REF", "").strip()
    socket_path = os.environ.get("AI16T_SECRET_SOCKET", "").strip()
    if not REFERENCE_PATTERN.fullmatch(reference) or not socket_path:
        raise RuntimeError("probe configuration is invalid")
    resolver = UnixSecretResolver(socket_path, {reference})
    resolver.ready()
    provider = APIYIProvider(
        "https://api.apiyi.com/v1",
        resolver,
        reference,
        float(os.environ.get("AI16T_PROVIDER_TIMEOUT_SECONDS", "20")),
    )
    result = provider.probe_models()
    result.update(
        {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "vps_egress_ip": os.environ.get("AI99T_EXPECTED_EGRESS_IP", ""),
            "token_status": os.environ.get("AI16T_DIAGNOSTIC_TOKEN_STATUS", "unknown"),
            "token_expiry": os.environ.get("AI16T_DIAGNOSTIC_TOKEN_EXPIRY", "unknown"),
            "quota_state": os.environ.get("AI16T_DIAGNOSTIC_QUOTA_STATE", "unknown"),
            "group": os.environ.get("AI16T_DIAGNOSTIC_GROUP", "unknown"),
            "model_whitelist_state": os.environ.get(
                "AI16T_DIAGNOSTIC_MODEL_WHITELIST", "unknown"
            ),
            "ip_whitelist_state": os.environ.get(
                "AI16T_DIAGNOSTIC_IP_WHITELIST", "unknown"
            ),
        }
    )
    encoded = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if SECRET_PATTERN.search(encoded) or "Bearer " in encoded:
        raise RuntimeError("probe output failed secret scan")
    return result


def main() -> int:
    try:
        result = run_probe()
    except Exception as error:  # noqa: BLE001 - CLI must emit only a sanitized failure type.
        print(
            json.dumps(
                {"probe_status": "failed", "error": type(error).__name__},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    result["probe_status"] = (
        "pass" if 200 <= int(result["upstream_http_status"]) < 300 else "auth_rejected"
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["probe_status"] == "pass" else 3


if __name__ == "__main__":
    sys.exit(main())
