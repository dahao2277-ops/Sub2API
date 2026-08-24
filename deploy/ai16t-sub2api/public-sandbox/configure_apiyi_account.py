from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".runtime" / "public.env"
COMPOSE_FILE = ROOT / "compose.public-sandbox.yml"
SECRET_REFERENCE = "apiyi/prod-canary"


def compose(*arguments: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(COMPOSE_FILE),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    )


def load_metadata() -> dict[str, Any]:
    completed = compose(
        "exec",
        "-T",
        "secret-provider",
        "/app/ai16t-secret-provider",
        "metadata",
        "--ref",
        SECRET_REFERENCE,
    )
    metadata = json.loads(completed.stdout)
    if (
        not isinstance(metadata, dict)
        or metadata.get("secret_ref") != SECRET_REFERENCE
        or not isinstance(metadata.get("fingerprint"), str)
        or len(metadata["fingerprint"]) != 64
        or not isinstance(metadata.get("last4"), str)
        or len(metadata["last4"]) != 4
        or "value" in metadata
    ):
        raise RuntimeError("SecretProvider metadata is invalid")
    return metadata


def configure(metadata: dict[str, Any]) -> None:
    credentials = {
        "secret_ref": SECRET_REFERENCE,
        "fingerprint": metadata["fingerprint"],
        "last4": metadata["last4"],
        "created_at": metadata["created_at"],
        "rotated_at": metadata["rotated_at"],
        "base_url": "https://api.apiyi.com/v1",
        "api_protocol": "chat_completions",
    }
    extra = {
        "ai16t_managed": True,
        "canary_only": True,
        "scheduler_route": "commercial-core-only",
        "rpm_limit": 10,
        "retry_limit": 1,
        "sticky_routing": True,
    }
    sql = """
BEGIN;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM accounts
    WHERE name <> 'APIYI-CANARY-OPENAI' AND deleted_at IS NULL
  ) THEN
    RAISE EXCEPTION 'unexpected provider account already exists';
  END IF;
END $$;
INSERT INTO accounts(name,platform,type,credentials,extra,concurrency,priority,status)
SELECT
  'APIYI-CANARY-OPENAI','openai','apikey',
  :'credentials'::jsonb, :'extra'::jsonb, 2, 100, 'disabled'
WHERE NOT EXISTS (
  SELECT 1 FROM accounts
  WHERE name='APIYI-CANARY-OPENAI' AND deleted_at IS NULL
);
UPDATE accounts SET
  platform='openai', type='apikey', credentials=:'credentials'::jsonb,
  extra=:'extra'::jsonb, concurrency=2, priority=100, status='disabled',
  error_message=NULL, updated_at=NOW()
WHERE name='APIYI-CANARY-OPENAI' AND deleted_at IS NULL;
DO $$
BEGIN
  IF (SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL) <> 1 THEN
    RAISE EXCEPTION 'APIYI account metadata cardinality check failed';
  END IF;
  IF EXISTS (
    SELECT 1 FROM accounts
    WHERE credentials ? 'api_key'
       OR credentials ? 'key'
       OR credentials ? 'access_token'
       OR credentials->>'secret_ref' <> 'apiyi/prod-canary'
  ) THEN
    RAISE EXCEPTION 'plaintext or unexpected credential metadata detected';
  END IF;
END $$;
COMMIT;
"""
    compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        "credentials=" + json.dumps(credentials, separators=(",", ":")),
        "-v",
        "extra=" + json.dumps(extra, separators=(",", ":")),
        "-U",
        "ai99t_sub2api",
        "-d",
        "ai99t_sub2api",
        input_text=sql,
    )


if __name__ == "__main__":
    configure(load_metadata())
    print("APIYI_UPSTREAM_METADATA_ACCOUNT=PASS status=disabled")
