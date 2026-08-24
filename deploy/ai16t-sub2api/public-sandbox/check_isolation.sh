#!/bin/sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$root/compose.public-sandbox.yml"
caddy="$root/Caddyfile"

require_text() {
  needle="$1"
  file="$2"
  grep -Fq -- "$needle" "$file" || {
    echo "Missing required isolation marker: $needle" >&2
    exit 1
  }
}

for marker in \
  'name: ai99t-platform-b-public-sandbox' \
  'name: ai99t-platform-b-private' \
  'name: ai99t-platform-b-edge' \
  'DATABASE_DBNAME: ai99t_sub2api' \
  'AI16T_ISOLATED_TEST_MODE: "false"' \
  'AI16T_ISOLATED_TEST_HOOKS_ENABLED: "false"' \
  'SECURITY_URL_ALLOWLIST_ENABLED: "true"' \
  'SECURITY_URL_ALLOWLIST_ALLOW_INSECURE_HTTP: "false"' \
  'SECURITY_URL_ALLOWLIST_ALLOW_PRIVATE_HOSTS: "false"' \
  '127.0.0.1:${BLUE_LOOPBACK_PORT:-18181}:8080' \
  '127.0.0.1:${GREEN_LOOPBACK_PORT:-18182}:8080' \
  'TP_RUNTIME_MODE: ${AI16T_CORE_RUNTIME_MODE:-isolated-test}' \
  'TP_TEST_MODE: ${AI16T_CORE_TEST_MODE:-mock-only-enabled}' \
  'AI16T_PROVIDER_BASE_URL: https://api.apiyi.com/v1' \
  'AI16T_SECRET_SOCKET: /run/ai16t-secret/provider.sock' \
  'internal: true'; do
  require_text "$marker" "$compose"
done

require_text '{$AI99T_DOMAIN}' "$caddy"
require_text 'reverse_proxy {$AI99T_ACTIVE_UPSTREAM}' "$caddy"

if grep -Eq 'ai16t\.com|token-platform-public-preview|global-market-ai-sg|143\.198\.206\.155|178\.128\.96\.199' "$compose" "$caddy"; then
  echo "Platform A reference detected" >&2
  exit 1
fi

if grep -Eq 'api\.(openai|anthropic)\.com|stripe|paypal|adyen|braintree' "$compose"; then
  echo "Real provider or payment surface detected" >&2
  exit 1
fi

echo "PUBLIC_SANDBOX_ISOLATION=PASS"
