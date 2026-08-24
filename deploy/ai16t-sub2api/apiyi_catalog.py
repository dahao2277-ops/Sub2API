from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from secret_provider_client import UnixSecretResolver

APIYI_MODELS_URL = "https://api.apiyi.com/v1/models"
MAX_CATALOG_BYTES = 8 * 1024 * 1024
SECRET_LIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
SENSITIVE_FIELDS = {"authorization", "api_key", "apikey", "key", "secret", "token"}


def _sanitize(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item, secret)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize(item, secret) for item in value]
    if isinstance(value, str):
        if secret in value or SECRET_LIKE.search(value):
            return "[REDACTED]"
    return value


def fetch_snapshot(output_path: Path) -> dict[str, Any]:
    reference = os.environ.get("AI16T_PROVIDER_SECRET_REF", "apiyi/integration")
    resolver = UnixSecretResolver(os.environ["AI16T_SECRET_SOCKET"], {reference})
    material = resolver.resolve(reference)
    request = urllib.request.Request(
        APIYI_MODELS_URL,
        headers={
            "Authorization": "Bearer " + material.reveal(),
            "Accept": "application/json",
            "User-Agent": "AI16T-Platform-B-Catalog/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(MAX_CATALOG_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"APIYI model catalog HTTP {int(error.code)}") from error
    except (TimeoutError, urllib.error.URLError) as error:
        raise RuntimeError("APIYI model catalog is unavailable") from error
    if len(body) > MAX_CATALOG_BYTES:
        raise RuntimeError("APIYI model catalog exceeds the safety limit")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError("APIYI model catalog is invalid JSON") from error
    sanitized = _sanitize(payload, material.reveal())
    models = sanitized.get("data") if isinstance(sanitized, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("APIYI model catalog does not contain a model list")
    snapshot = {
        "source": APIYI_MODELS_URL,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_count": len(models),
        "data": models,
    }
    encoded = json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if material.reveal() in encoded or SECRET_LIKE.search(encoded):
        raise RuntimeError("APIYI model catalog secret scan failed")
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_path.write_text(encoded, encoding="utf-8")
    output_path.chmod(0o600)
    return snapshot


if __name__ == "__main__":
    target = Path(os.environ.get("APIYI_MODEL_CATALOG_OUTPUT", "APIYI_MODEL_CATALOG_SNAPSHOT.json"))
    result = fetch_snapshot(target)
    print(f"APIYI_MODEL_CATALOG=PASS count={result['model_count']}")
