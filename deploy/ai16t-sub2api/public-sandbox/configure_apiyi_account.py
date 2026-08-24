from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".runtime" / "public.env"
COMPOSE_FILE = ROOT / "compose.public-sandbox.yml"
SECRET_REFERENCE = "apiyi/prod-canary"
ACCOUNT_NAME = "APIYI-PROD-CANARY"
CANARY_MODELS = ("deepseek-chat", "gpt-5.6-luna")


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
        "allowed_models": list(CANARY_MODELS),
        "customer_concurrency": 1,
        "first_customer_upstream_spend_limit_usd": 1,
        "rpm_limit": 10,
        "retry_limit": 1,
        "retry_4xx": False,
        "cooldown_429": True,
        "sticky_routing": True,
    }
    sql = """
BEGIN;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM accounts
    WHERE name <> 'APIYI-PROD-CANARY' AND deleted_at IS NULL
  ) THEN
    RAISE EXCEPTION 'unexpected provider account already exists';
  END IF;
END $$;
INSERT INTO accounts(name,platform,type,credentials,extra,concurrency,priority,status)
SELECT
  'APIYI-PROD-CANARY','openai','apikey',
  :'credentials'::jsonb, :'extra'::jsonb, 2, 100, 'disabled'
WHERE NOT EXISTS (
  SELECT 1 FROM accounts
  WHERE name='APIYI-PROD-CANARY' AND deleted_at IS NULL
);
UPDATE accounts SET
  platform='openai', type='apikey', credentials=:'credentials'::jsonb,
  extra=:'extra'::jsonb, concurrency=2, priority=100, status='disabled',
  error_message=NULL, updated_at=NOW()
WHERE name='APIYI-PROD-CANARY' AND deleted_at IS NULL;
INSERT INTO groups(
  name,description,rate_multiplier,is_exclusive,status,platform,
  subscription_type,daily_limit_usd,default_validity_days,model_routing,
  model_routing_enabled,rpm_limit,allow_image_generation,
  allow_batch_image_generation,video_rate_independent,allow_live,
  models_list_config,profit_control_enabled
)
SELECT
  'CANARY-CUSTOMER-01','First customer manual-approval canary',1.0,true,'active','openai',
  'standard',1.0,7,'{}'::jsonb,false,10,false,false,false,false,
  '{"enabled":true,"models":["deepseek-chat","gpt-5.6-luna"]}'::jsonb,false
WHERE NOT EXISTS (
  SELECT 1 FROM groups WHERE name='CANARY-CUSTOMER-01' AND deleted_at IS NULL
);
UPDATE groups SET
  description='First customer manual-approval canary',
  rate_multiplier=1.0,is_exclusive=true,status='active',platform='openai',
  subscription_type='standard',daily_limit_usd=1.0,weekly_limit_usd=NULL,
  monthly_limit_usd=NULL,default_validity_days=7,model_routing='{}'::jsonb,
  model_routing_enabled=false,rpm_limit=10,allow_image_generation=false,
  allow_batch_image_generation=false,video_rate_independent=false,
  allow_live=false,
  models_list_config='{"enabled":true,"models":["deepseek-chat","gpt-5.6-luna"]}'::jsonb,
  profit_control_enabled=false,updated_at=NOW()
WHERE name='CANARY-CUSTOMER-01' AND deleted_at IS NULL;
DELETE FROM account_groups
WHERE account_id=(SELECT id FROM accounts WHERE name='APIYI-PROD-CANARY' AND deleted_at IS NULL)
  AND group_id<>(SELECT id FROM groups WHERE name='CANARY-CUSTOMER-01' AND deleted_at IS NULL);
INSERT INTO account_groups(account_id,group_id,priority)
SELECT account.id,canary_group.id,100
FROM accounts account, groups canary_group
WHERE account.name='APIYI-PROD-CANARY' AND account.deleted_at IS NULL
  AND canary_group.name='CANARY-CUSTOMER-01' AND canary_group.deleted_at IS NULL
ON CONFLICT(account_id,group_id) DO UPDATE SET priority=excluded.priority;
INSERT INTO settings(key,value) VALUES
  ('registration_enabled','true'),
  ('email_verify_enabled','false'),
  ('promo_code_enabled','false'),
  ('affiliate_enabled','false'),
  ('purchase_subscription_enabled','false'),
  ('allow_ungrouped_key_scheduling','false'),
  ('auth_source_default_email_balance','0'),
  ('auth_source_default_email_grant_on_signup','false'),
  ('auth_source_default_email_subscriptions','[]')
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=NOW();
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
  IF (SELECT COUNT(*) FROM groups WHERE name='CANARY-CUSTOMER-01' AND deleted_at IS NULL) <> 1
     OR EXISTS (
       SELECT 1 FROM groups
       WHERE name='CANARY-CUSTOMER-01' AND deleted_at IS NULL AND (
            status <> 'active' OR NOT is_exclusive OR platform <> 'openai'
         OR daily_limit_usd <> 1.0 OR default_validity_days <> 7 OR rpm_limit <> 10
         OR allow_image_generation OR allow_batch_image_generation OR allow_live
         OR models_list_config <> '{"enabled":true,"models":["deepseek-chat","gpt-5.6-luna"]}'::jsonb
       )
     ) THEN
    RAISE EXCEPTION 'first customer canary group policy check failed';
  END IF;
  IF (SELECT COUNT(*) FROM account_groups ag
      JOIN accounts a ON a.id=ag.account_id
      JOIN groups g ON g.id=ag.group_id
      WHERE a.name='APIYI-PROD-CANARY' AND a.deleted_at IS NULL
        AND g.name='CANARY-CUSTOMER-01' AND g.deleted_at IS NULL) <> 1 THEN
    RAISE EXCEPTION 'canary account-group binding check failed';
  END IF;
  IF EXISTS (
    SELECT 1 FROM user_allowed_groups uag
    JOIN groups g ON g.id=uag.group_id
    WHERE g.name='CANARY-CUSTOMER-01' AND g.deleted_at IS NULL
  ) THEN
    RAISE EXCEPTION 'customer enrollment must not happen during preparation';
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
    print(
        "APIYI_UPSTREAM_METADATA_ACCOUNT=PASS "
        f"name={ACCOUNT_NAME} status=disabled group=CANARY-CUSTOMER-01 "
        f"models={','.join(CANARY_MODELS)}"
    )
