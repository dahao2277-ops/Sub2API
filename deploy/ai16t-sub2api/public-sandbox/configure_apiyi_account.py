from __future__ import annotations

import hmac
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".runtime" / "public.env"
COMPOSE_FILE = ROOT / "compose.public-sandbox.yml"
SECRET_REFERENCE = "apiyi/prod-canary"
ACCOUNT_NAME = "APIYI-PROD-CANARY"
CANARY_MODELS = ("deepseek-chat", "gpt-5.6-luna")
DATABASE_NAME = os.getenv("AI99T_SUB2API_DATABASE", "ai99t_sub2api")
if re.fullmatch(r"[A-Za-z0-9_]+", DATABASE_NAME) is None:
    raise RuntimeError("AI99T Sub2API database name is invalid")


def fingerprint_pin_path() -> Path:
    configured = os.getenv("AI99T_APIYI_KEY_B_FINGERPRINT_FILE", "").strip()
    if not configured and ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("AI99T_APIYI_KEY_B_FINGERPRINT_FILE="):
                configured = line.split("=", 1)[1].strip()
                break
    if not configured:
        raise RuntimeError("APIYI Key B fingerprint pin path is not configured")
    return Path(configured)


def load_expected_fingerprint() -> str:
    path = fingerprint_pin_path()
    details = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise RuntimeError("APIYI Key B fingerprint pin must be a regular file")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise RuntimeError("APIYI Key B fingerprint pin must have mode 0600")
    fingerprint = path.read_text().strip()
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise RuntimeError("APIYI Key B fingerprint pin is invalid")
    return fingerprint


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


def configure(metadata: dict[str, Any], expected_fingerprint: str) -> None:
    actual_fingerprint = str(metadata.get("fingerprint", ""))
    if re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint) is None or not hmac.compare_digest(
        actual_fingerprint, expected_fingerprint
    ):
        raise RuntimeError("SecretProvider metadata does not match approved APIYI Key B")
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
DO $$
DECLARE
  internal_user_id bigint;
  canary_group_id bigint;
BEGIN
  SELECT k.user_id INTO internal_user_id
  FROM api_keys k JOIN users u ON u.id=k.user_id
  WHERE k.name=('ai99t-internal-' || 'canary-20260824')
    AND k.status='active' AND u.role='admin' AND u.status='active';
  IF internal_user_id IS NULL OR (
    SELECT COUNT(*) FROM api_keys
    WHERE name=('ai99t-internal-' || 'canary-20260824')
  ) <> 1 THEN
    RAISE EXCEPTION 'expected one active internal admin Canary API key';
  END IF;
  SELECT id INTO canary_group_id FROM groups
  WHERE name='CANARY-CUSTOMER-01' AND deleted_at IS NULL;
  UPDATE users SET concurrency=1,updated_at=NOW() WHERE id=internal_user_id;
  UPDATE api_keys SET group_id=canary_group_id,updated_at=NOW()
  WHERE name=('ai99t-internal-' || 'canary-20260824') AND user_id=internal_user_id;
  INSERT INTO user_allowed_groups(user_id,group_id)
  VALUES (internal_user_id,canary_group_id)
  ON CONFLICT(user_id,group_id) DO NOTHING;
END $$;
INSERT INTO settings(key,value) VALUES
  ('registration_enabled','true'),
  ('email_verify_enabled','false'),
  ('promo_code_enabled','false'),
  ('affiliate_enabled','false'),
  ('purchase_subscription_enabled','false'),
  ('payment_enabled','false'),
  ('BALANCE_PAYMENT_DISABLED','true'),
  ('ENABLED_PAYMENT_TYPES',''),
  ('payment_visible_method_alipay_enabled','false'),
  ('payment_visible_method_wxpay_enabled','false'),
  ('allow_ungrouped_key_scheduling','false'),
  ('default_balance','0'),
  ('default_concurrency','1'),
  ('default_subscriptions','[]'),
  ('auth_source_default_email_balance','0'),
  ('auth_source_default_email_concurrency','1'),
  ('auth_source_default_email_grant_on_signup','true'),
  ('auth_source_default_email_grant_on_first_bind','false'),
  ('auth_source_default_email_subscriptions','[]'),
  ('auth_source_default_linuxdo_balance','0'),
  ('auth_source_default_linuxdo_concurrency','1'),
  ('auth_source_default_linuxdo_grant_on_signup','false'),
  ('auth_source_default_linuxdo_grant_on_first_bind','false'),
  ('auth_source_default_linuxdo_subscriptions','[]'),
  ('auth_source_default_oidc_balance','0'),
  ('auth_source_default_oidc_concurrency','1'),
  ('auth_source_default_oidc_grant_on_signup','false'),
  ('auth_source_default_oidc_grant_on_first_bind','false'),
  ('auth_source_default_oidc_subscriptions','[]'),
  ('auth_source_default_wechat_balance','0'),
  ('auth_source_default_wechat_concurrency','1'),
  ('auth_source_default_wechat_grant_on_signup','false'),
  ('auth_source_default_wechat_grant_on_first_bind','false'),
  ('auth_source_default_wechat_subscriptions','[]'),
  ('auth_source_default_github_balance','0'),
  ('auth_source_default_github_concurrency','1'),
  ('auth_source_default_github_grant_on_signup','false'),
  ('auth_source_default_github_grant_on_first_bind','false'),
  ('auth_source_default_github_subscriptions','[]'),
  ('auth_source_default_google_balance','0'),
  ('auth_source_default_google_concurrency','1'),
  ('auth_source_default_google_grant_on_signup','false'),
  ('auth_source_default_google_grant_on_first_bind','false'),
  ('auth_source_default_google_subscriptions','[]'),
  ('auth_source_default_dingtalk_balance','0'),
  ('auth_source_default_dingtalk_concurrency','1'),
  ('auth_source_default_dingtalk_grant_on_signup','false'),
  ('auth_source_default_dingtalk_grant_on_first_bind','false'),
  ('auth_source_default_dingtalk_subscriptions','[]')
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=NOW();
UPDATE payment_provider_instances
SET enabled=false,refund_enabled=false,allow_user_refund=false,updated_at=NOW()
WHERE enabled OR refund_enabled OR allow_user_refund;
DO $$
BEGIN
  IF (SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL) <> 1 THEN
    RAISE EXCEPTION 'APIYI account metadata cardinality check failed';
  END IF;
  IF EXISTS (
    SELECT 1 FROM accounts
    WHERE credentials ? 'api_key'
       OR credentials ? 'key'
       OR credentials ? ('access_' || 'token')
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
    JOIN users u ON u.id=uag.user_id
    WHERE g.name='CANARY-CUSTOMER-01' AND g.deleted_at IS NULL
      AND u.role <> 'admin'
  ) THEN
    RAISE EXCEPTION 'customer enrollment must not happen during preparation';
  END IF;
  IF EXISTS (
    SELECT 1 FROM settings WHERE
       (key='payment_enabled' AND value<>'false')
    OR (key='BALANCE_PAYMENT_DISABLED' AND value<>'true')
    OR (key='ENABLED_PAYMENT_TYPES' AND value<>'')
    OR (key IN ('payment_visible_method_alipay_enabled','payment_visible_method_wxpay_enabled')
        AND value<>'false')
  ) OR EXISTS (
    SELECT 1 FROM payment_provider_instances
    WHERE enabled OR refund_enabled OR allow_user_refund
  ) THEN
    RAISE EXCEPTION 'payment surfaces must remain fully disabled';
  END IF;
  IF (SELECT COUNT(*) FROM api_keys k JOIN users u ON u.id=k.user_id
      JOIN groups g ON g.id=k.group_id
      WHERE k.name=('ai99t-internal-' || 'canary-20260824') AND k.status='active'
        AND u.role='admin' AND u.status='active' AND u.concurrency=1
        AND g.name='CANARY-CUSTOMER-01' AND g.deleted_at IS NULL
        AND k.expires_at IS NOT NULL
        AND (SELECT COUNT(*) FROM api_keys all_keys
             WHERE all_keys.user_id=k.user_id AND all_keys.deleted_at IS NULL) = 1
        AND k.expires_at <= k.created_at + INTERVAL '7 days 5 minutes'
        AND k.expires_at > NOW()) <> 1 THEN
    RAISE EXCEPTION 'internal Canary test identity policy check failed';
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
        DATABASE_NAME,
        input_text=sql,
    )


if __name__ == "__main__":
    configure(load_metadata(), load_expected_fingerprint())
    print(
        "APIYI_UPSTREAM_METADATA_ACCOUNT=PASS "
        f"name={ACCOUNT_NAME} status=disabled group=CANARY-CUSTOMER-01 "
        f"models={','.join(CANARY_MODELS)}"
    )
