#!/usr/bin/env bash
# Creates the secrets accountservice and its callers need. WRITTEN BUT NOT RUN: requires explicit approval.
# Generates values locally and never prints them. Idempotent: an existing secret is left untouched unless
# FORCE=1 (rotating a service secret requires restarting accountservice AND the caller together).
#
# Secret accountservice-credentials (namespace online-boutique) keys:
#   ACCOUNT_SERVICE_SECRETS  "frontend:<s1>,shoppingassistantservice:<s2>,checkoutservice:<s3>"  (accountservice only)
#   FRONTEND_SECRET          <s1>   -> frontend      ACCOUNT_SERVICE_SECRET
#   ASSISTANT_SECRET         <s2>   -> shoppingassistantservice ACCOUNT_SERVICE_SECRET
#   CHECKOUT_SECRET          <s3>   -> checkoutservice ACCOUNT_SERVICE_SECRET
#   TELEMETRY_REF_KEY        HMAC key for app.session.ref (the SAME value in every service; rotating it
#                            changes every session reference, which only breaks continuity of those refs)
#   CHECKOUT_FP_KEY          HMAC key for checkout request fingerprints (checkoutservice only)
set -euo pipefail
NS="${NAMESPACE:-online-boutique}"
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }

if kubectl -n "$NS" get secret accountservice-credentials >/dev/null 2>&1 && [ "${FORCE:-0}" != 1 ]; then
  echo "accountservice-credentials exists; leaving it"
  exit 0
fi
S1="$(rand)"; S2="$(rand)"; S3="$(rand)"
kubectl -n "$NS" create secret generic accountservice-credentials \
  --from-literal=ACCOUNT_SERVICE_SECRETS="frontend:${S1},shoppingassistantservice:${S2},checkoutservice:${S3}" \
  --from-literal=FRONTEND_SECRET="$S1" --from-literal=ASSISTANT_SECRET="$S2" --from-literal=CHECKOUT_SECRET="$S3" \
  --from-literal=TELEMETRY_REF_KEY="$(rand)" --from-literal=CHECKOUT_FP_KEY="$(rand)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "created accountservice-credentials"
