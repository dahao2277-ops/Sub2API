#!/bin/sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$root/compose.public-sandbox.yml"
env_file="$root/.runtime/public.env"

[ -f "$env_file" ] || { echo "Runtime is not prepared" >&2; exit 1; }

docker compose --env-file "$env_file" -f "$compose" exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U ai99t_sub2api -d ai99t_sub2api <<'SQL'
INSERT INTO settings(key,value) VALUES
  ('registration_enabled','false'),
  ('email_verify_enabled','false'),
  ('promo_code_enabled','false'),
  ('password_reset_enabled','false'),
  ('invitation_code_enabled','false'),
  ('affiliate_enabled','false'),
  ('purchase_subscription_enabled','false'),
  ('site_name','AI99T Platform B · Mock Sandbox'),
  ('frontend_url','https://ai99t.com'),
  ('api_key_acl_trust_forwarded_ip','false')
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=NOW();

DO $$
DECLARE
  admin_count integer;
  provider_count integer;
BEGIN
  SELECT COUNT(*) INTO admin_count FROM users WHERE role='admin' AND status='active';
  IF admin_count <> 1 THEN
    RAISE EXCEPTION 'expected exactly one active admin, found %', admin_count;
  END IF;
  SELECT COUNT(*) INTO provider_count FROM accounts;
  IF provider_count <> 0 THEN
    RAISE EXCEPTION 'real provider/account rows are forbidden in public sandbox';
  END IF;
END $$;
SQL

echo "PUBLIC_SANDBOX_BOOTSTRAP_POLICY=PASS"
