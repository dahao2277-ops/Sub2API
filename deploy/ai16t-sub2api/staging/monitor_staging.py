from __future__ import annotations

import json
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
ENV_FILE = RUNTIME / "staging.env"
COMPOSE_FILE = ROOT / "compose.staging.yml"


def compose_json(output: str) -> list[dict[str, Any]]:
    stripped = output.strip()
    if not stripped:
        return []
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return [json.loads(line) for line in stripped.splitlines() if line.strip()]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    raise RuntimeError("unexpected Docker Compose JSON output")


def endpoint(url: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            response.read(2 * 1024 * 1024)
            return {
                "status": response.status,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
    except (urllib.error.URLError, TimeoutError) as error:
        return {
            "status": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": type(error).__name__,
        }


def main() -> None:
    compose_status = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(COMPOSE_FILE),
            "ps",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    containers = compose_json(compose_status)
    snapshot = {
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "blue_health": endpoint("http://127.0.0.1:18181/health"),
        "blue_ready": endpoint("http://127.0.0.1:18181/ready"),
        "green_health": endpoint("http://127.0.0.1:18182/health"),
        "green_ready": endpoint("http://127.0.0.1:18182/ready"),
        "containers": [
            {
                "service": item.get("Service"),
                "state": item.get("State"),
                "health": item.get("Health", ""),
            }
            for item in containers
        ],
    }
    healthy_endpoints = all(
        snapshot[key]["status"] == 200
        for key in ("blue_health", "blue_ready", "green_health", "green_ready")
    )
    healthy_containers = all(
        item["state"] == "running" and item["health"] == "healthy"
        for item in snapshot["containers"]
    )
    snapshot["result"] = "PASS" if healthy_endpoints and healthy_containers else "FAIL"
    output = RUNTIME / "monitoring_snapshot.json"
    output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    if stat.S_IMODE(output.stat().st_mode) != 0o600:
        raise RuntimeError("monitoring evidence mode is not 0600")
    print(f"STAGING_MONITOR={snapshot['result']}")
    if snapshot["result"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
