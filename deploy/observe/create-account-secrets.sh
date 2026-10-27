#!/usr/bin/env bash
# Creates the secrets the accounts database needs. NOT run automatically: requires explicit approval.
# Generates values locally and never prints them. Idempotent: existing secrets are left untouched
# unless FORCE=1 (rotating accounts-db-credentials after first start does NOT change roles inside an
# initialised database - see docs/ai-demo.md).
set -euo pipefail
NS="${NAMESPACE:-online-boutique}"
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

exists() { kubectl -n "$NS" get secret "$1" >/dev/null 2>&1; }

if exists shopping-assistant-account-api && [ "${FORCE:-0}" != 1 ]; then
  echo "shopping-assistant-account-api exists; leaving it"
else
  kubectl -n "$NS" create secret generic shopping-assistant-account-api \
    --from-literal=ACCOUNT_API_SECRET="$(rand)" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "created shopping-assistant-account-api"
fi

if exists accounts-db-credentials && [ "${FORCE:-0}" != 1 ]; then
  echo "accounts-db-credentials exists; leaving it"
else
  APP_PW="$(rand)"; EXP_PW="$(rand)"; SU_PW="$(rand)"
  kubectl -n "$NS" create secret generic accounts-db-credentials \
    --from-literal=POSTGRES_PASSWORD="$SU_PW" \
    --from-literal=APP_DB_PASSWORD="$APP_PW" \
    --from-literal=EXPORTER_DB_PASSWORD="$EXP_PW" \
    --from-literal=ACCOUNTS_DATABASE_URL="postgresql://boutique_app:${APP_PW}@accounts-db.${NS}.svc.cluster.local:5432/accounts?connect_timeout=2" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "created accounts-db-credentials"
fi
