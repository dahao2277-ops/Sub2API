#!/bin/sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$root/compose.public-sandbox.yml"
env_file="$root/.runtime/public.env"

[ -f "$env_file" ] || { echo "Runtime is not prepared" >&2; exit 1; }

provider_mode="$(sed -n 's/^AI16T_PROVIDER_MODE=//p' "$env_file")"
case "$provider_mode" in
  mock)
    site_name='AI99T Platform B · Mock Sandbox'
    ;;
  apiyi)
    site_name='AI99T Platform B · Canary'
    ;;
  *)
    echo "Unsupported provider mode" >&2
    exit 1
    ;;
esac

docker compose --env-file "$env_file" -f "$compose" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -v provider_mode="$provider_mode" -v site_name="$site_name" \
  -U ai99t_sub2api -d ai99t_sub2api <<'SQL'
BEGIN;
INSERT INTO settings(key,value) VALUES
  ('registration_enabled','true'),
  ('email_verify_enabled','false'),
  ('promo_code_enabled','false'),
  ('password_reset_enabled','false'),
  ('invitation_code_enabled','false'),
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
  ('auth_source_default_dingtalk_subscriptions','[]'),
  ('site_name', :'site_name'),
  ('frontend_url','https://ai99t.com'),
  ('api_key_acl_trust_forwarded_ip','false')
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=NOW();

UPDATE payment_provider_instances
SET enabled=false,refund_enabled=false,allow_user_refund=false,updated_at=NOW()
WHERE enabled OR refund_enabled OR allow_user_refund;

SELECT set_config('ai16t.provider_mode', :'provider_mode', false);

DO $$
DECLARE
  admin_count integer;
  provider_count integer;
BEGIN
  SELECT COUNT(*) INTO admin_count FROM users WHERE role='admin' AND status='active';
  IF admin_count <> 1 THEN
    RAISE EXCEPTION 'expected exactly one active admin, found %', admin_count;
  END IF;
  SELECT COUNT(*) INTO provider_count FROM accounts WHERE deleted_at IS NULL;
  IF current_setting('ai16t.provider_mode') = 'mock' AND provider_count <> 0 THEN
    RAISE EXCEPTION 'real provider/account rows are forbidden in public sandbox';
  END IF;
  IF current_setting('ai16t.provider_mode') = 'apiyi' AND provider_count <> 1 THEN
    RAISE EXCEPTION 'expected exactly one disabled APIYI metadata account, found %', provider_count;
  END IF;
  IF current_setting('ai16t.provider_mode') = 'apiyi' AND EXISTS (
    SELECT 1 FROM accounts
    WHERE deleted_at IS NULL AND (
          name <> 'APIYI-PROD-CANARY'
       OR platform <> 'openai'
       OR type <> 'apikey'
       OR status <> 'disabled'
       OR credentials ? 'api_key'
       OR credentials ? 'key'
       OR credentials ? ('access_' || 'token')
       OR credentials->>'secret_ref' <> 'apiyi/prod-canary'
    )
  ) THEN
    RAISE EXCEPTION 'unexpected provider account state in APIYI Canary';
  END IF;
  IF current_setting('ai16t.provider_mode') = 'apiyi' AND NOT EXISTS (
    SELECT 1 FROM groups
    WHERE name='CANARY-CUSTOMER-01' AND deleted_at IS NULL
      AND status='active' AND is_exclusive AND platform='openai'
      AND daily_limit_usd=1.0 AND default_validity_days=7 AND rpm_limit=10
      AND NOT allow_image_generation AND NOT allow_batch_image_generation
      AND NOT allow_live
      AND models_list_config='{"enabled":true,"models":["deepseek-chat","gpt-5.6-luna"]}'::jsonb
  ) THEN
    RAISE EXCEPTION 'first customer canary group policy is missing';
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
END $$;
COMMIT;
SQL

echo "PUBLIC_SANDBOX_BOOTSTRAP_POLICY=PASS"
