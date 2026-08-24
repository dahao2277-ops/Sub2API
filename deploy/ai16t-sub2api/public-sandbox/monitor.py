from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
ENV_FILE = RUNTIME / "public.env"
COMPOSE_FILE = ROOT / "compose.public-sandbox.yml"


def endpoint(url: str) -> dict[str, object]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read(1024 * 1024)
            return {"status": response.status, "body": body.decode("utf-8", "replace")}
    except (OSError, urllib.error.URLError) as error:
        return {"status": 0, "error": type(error).__name__}


def compose_ps() -> object:
    completed = subprocess.run(
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
    )
    stripped = completed.stdout.strip()
    if not stripped:
        return []
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return [json.loads(line) for line in stripped.splitlines() if line.strip()]


snapshot = {
    "checked_at": datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).isoformat(timespec="seconds"),
    "blue_health": endpoint("http://127.0.0.1:18181/health"),
    "blue_ready": endpoint("http://127.0.0.1:18181/ready"),
    "green_health": endpoint("http://127.0.0.1:18182/health"),
    "green_ready": endpoint("http://127.0.0.1:18182/ready"),
    "containers": compose_ps(),
}
endpoint_ok = all(
    snapshot[key].get("status") == 200
    for key in ("blue_health", "blue_ready", "green_health", "green_ready")
)
snapshot["result"] = "PASS" if endpoint_ok else "FAIL"
RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
output = RUNTIME / "monitor.json"
output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
output.chmod(0o600)
print(f"PUBLIC_SANDBOX_MONITOR={snapshot['result']}")
if not endpoint_ok:
    raise SystemExit(1)
